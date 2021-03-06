from numba import config, ir, ir_utils, utils, prange
import types
from numba.ir_utils import (
    mk_unique_var,
    next_label,
    add_offset_to_labels,
    replace_vars,
    remove_dels,
    remove_dead,
    rename_labels,
    find_topo_order,
    merge_adjacent_blocks)

from numba.analysis import compute_cfg_from_blocks
from numba.targets.rangeobj import range_iter_len
from numba.unsafe.ndarray import empty_inferred as unsafe_empty_inferred
import numba.types as nbtypes
import numpy as np

"""
Variable enable_inline_arraycall is only used for testing purpose.
"""
enable_inline_arraycall = True


class InlineClosureCallPass(object):
    """InlineClosureCallPass class looks for direct calls to locally defined
    closures, and inlines the body of the closure function to the call site.
    """

    def __init__(self, func_ir, flags, run_frontend):
        self.func_ir = func_ir
        self.flags = flags
        self.run_frontend = run_frontend

    def run(self):
        """Run inline closure call pass.
        """
        modified = False
        work_list = list(self.func_ir.blocks.items())
        debug_print = _make_debug_print("InlineClosureCallPass")
        debug_print("START")
        while work_list:
            label, block = work_list.pop()
            for i in range(len(block.body)):
                instr = block.body[i]
                if isinstance(instr, ir.Assign):
                    lhs = instr.target
                    expr = instr.value
                    if isinstance(expr, ir.Expr) and expr.op == 'call':
                        func_def = guard(_get_definition, self.func_ir, expr.func)
                        debug_print("found call to ", expr.func, " def = ", func_def)
                        if isinstance(func_def, ir.Expr) and func_def.op == "make_function":
                            new_blocks = self.inline_closure_call(block, i, func_def)
                            for block in new_blocks:
                                work_list.append(block)
                            modified = True
                            # current block is modified, skip the rest
                            break

        if enable_inline_arraycall:
            # Identify loop structure
            if modified:
                # Need to do some cleanups if closure inlining kicked in
                merge_adjacent_blocks(self.func_ir)
            cfg = compute_cfg_from_blocks(self.func_ir.blocks)
            debug_print("start inline arraycall")
            _debug_dump(cfg)
            loops = cfg.loops()
            sized_loops = [(k, len(loops[k].body)) for k in loops.keys()]
            visited = []
            # We go over all loops, bigger loops first (outer first)
            for k, s in sorted(sized_loops, key=lambda tup: tup[1], reverse=True):
                visited.append(k)
                if guard(_inline_arraycall, self.func_ir, cfg, visited, loops[k],
                        self.flags.auto_parallel):
                    modified = True
            if modified:
                _fix_nested_array(self.func_ir)

        if modified:
            remove_dels(self.func_ir.blocks)
            # repeat dead code elimintation until nothing can be further
            # removed
            while (remove_dead(self.func_ir.blocks, self.func_ir.arg_names)):
                pass
            self.func_ir.blocks = rename_labels(self.func_ir.blocks)
        debug_print("END")

    def inline_closure_call(self, block, i, callee):
        """Inline the body of `callee` at its callsite (`i`-th instruction of `block`)
        """
        scope = block.scope
        instr = block.body[i]
        call_expr = instr.value
        debug_print = _make_debug_print("inline_closure_call")
        debug_print("Found closure call: ", instr, " with callee = ", callee)
        func_ir = self.func_ir
        # first, get the IR of the callee
        callee_ir = self.get_ir_of_code(callee.code)
        callee_blocks = callee_ir.blocks

        # 1. relabel callee_ir by adding an offset
        max_label = max(func_ir.blocks.keys())
        callee_blocks = add_offset_to_labels(callee_blocks, max_label + 1)
        callee_ir.blocks = callee_blocks
        min_label = min(callee_blocks.keys())
        max_label = max(callee_blocks.keys())
        #    reset globals in ir_utils before we use it
        ir_utils._max_label = max_label
        debug_print("After relabel")
        _debug_dump(callee_ir)

        # 2. rename all local variables in callee_ir with new locals created in func_ir
        callee_scopes = _get_all_scopes(callee_blocks)
        debug_print("callee_scopes = ", callee_scopes)
        #    one function should only have one local scope
        assert(len(callee_scopes) == 1)
        callee_scope = callee_scopes[0]
        var_dict = {}
        for var in callee_scope.localvars._con.values():
            if not (var.name in callee.code.co_freevars):
                new_var = scope.define(mk_unique_var(var.name), loc=var.loc)
                var_dict[var.name] = new_var
        debug_print("var_dict = ", var_dict)
        replace_vars(callee_blocks, var_dict)
        debug_print("After local var rename")
        _debug_dump(callee_ir)

        # 3. replace formal parameters with actual arguments
        args = list(call_expr.args)
        if callee.defaults:
            debug_print("defaults = ", callee.defaults)
            if isinstance(callee.defaults, tuple): # Python 3.5
                args = args + list(callee.defaults)
            elif isinstance(callee.defaults, ir.Var) or isinstance(callee.defaults, str):
                defaults = func_ir.get_definition(callee.defaults)
                assert(isinstance(defaults, ir.Const))
                loc = defaults.loc
                args = args + [ir.Const(value=v, loc=loc)
                               for v in defaults.value]
            else:
                raise NotImplementedError(
                    "Unsupported defaults to make_function: {}".format(defaults))
        _replace_args_with(callee_blocks, args)
        debug_print("After arguments rename: ")
        _debug_dump(callee_ir)

        # 4. replace freevar with actual closure var
        if callee.closure:
            closure = func_ir.get_definition(callee.closure)
            assert(isinstance(closure, ir.Expr)
                   and closure.op == 'build_tuple')
            assert(len(callee.code.co_freevars) == len(closure.items))
            debug_print("callee's closure = ", closure)
            _replace_freevars(callee_blocks, closure.items)
            debug_print("After closure rename")
            _debug_dump(callee_ir)

        # 5. split caller blocks into two
        new_blocks = []
        new_block = ir.Block(scope, block.loc)
        new_block.body = block.body[i + 1:]
        new_label = next_label()
        func_ir.blocks[new_label] = new_block
        new_blocks.append((new_label, new_block))
        block.body = block.body[:i]
        block.body.append(ir.Jump(min_label, instr.loc))

        # 6. replace Return with assignment to LHS
        topo_order = find_topo_order(callee_blocks)
        _replace_returns(callee_blocks, instr.target, new_label)
        #    remove the old definition of instr.target too
        if (instr.target.name in func_ir._definitions):
            func_ir._definitions[instr.target.name] = []

        # 7. insert all new blocks, and add back definitions
        for label in topo_order:
            # block scope must point to parent's
            block = callee_blocks[label]
            block.scope = scope
            _add_definitions(func_ir, block)
            func_ir.blocks[label] = block
            new_blocks.append((label, block))
        debug_print("After merge in")
        _debug_dump(func_ir)

        return new_blocks

    def get_ir_of_code(self, fcode):
        """
        Compile a code object to get its IR.
        """
        glbls = self.func_ir.func_id.func.__globals__
        nfree = len(fcode.co_freevars)
        func_env = "\n".join(["  c_%d = None" % i for i in range(nfree)])
        func_clo = ",".join(["c_%d" % i for i in range(nfree)])
        func_arg = ",".join(["x_%d" % i for i in range(fcode.co_argcount)])
        func_text = "def g():\n%s\n  def f(%s):\n    return (%s)\n  return f" % (
            func_env, func_arg, func_clo)
        loc = {}
        exec(func_text, glbls, loc)

        # hack parameter name .0 for Python 3 versions < 3.6
        if utils.PYVERSION >= (3,) and utils.PYVERSION < (3, 6):
            co_varnames = list(fcode.co_varnames)
            if co_varnames[0] == ".0":
                co_varnames[0] = "implicit0"
            fcode = types.CodeType(
                fcode.co_argcount,
                fcode.co_kwonlyargcount,
                fcode.co_nlocals,
                fcode.co_stacksize,
                fcode.co_flags,
                fcode.co_code,
                fcode.co_consts,
                fcode.co_names,
                tuple(co_varnames),
                fcode.co_filename,
                fcode.co_name,
                fcode.co_firstlineno,
                fcode.co_lnotab,
                fcode.co_freevars,
                fcode.co_cellvars)

        f = loc['g']()
        f.__code__ = fcode
        f.__name__ = fcode.co_name
        ir = self.run_frontend(f)
        return ir

def _make_debug_print(prefix):
    def debug_print(*args):
        if config.DEBUG_INLINE_CLOSURE:
            print(prefix + ": " + "".join(str(x) for x in args))
    return debug_print

def _debug_dump(func_ir):
    if config.DEBUG_INLINE_CLOSURE:
        func_ir.dump()


def _get_all_scopes(blocks):
    """Get all block-local scopes from an IR.
    """
    all_scopes = []
    for label, block in blocks.items():
        if not (block.scope in all_scopes):
            all_scopes.append(block.scope)
    return all_scopes


def _replace_args_with(blocks, args):
    """
    Replace ir.Arg(...) with real arguments from call site
    """
    for label, block in blocks.items():
        assigns = block.find_insts(ir.Assign)
        for stmt in assigns:
            if isinstance(stmt.value, ir.Arg):
                idx = stmt.value.index
                assert(idx < len(args))
                stmt.value = args[idx]


def _replace_freevars(blocks, args):
    """
    Replace ir.FreeVar(...) with real variables from parent function
    """
    for label, block in blocks.items():
        assigns = block.find_insts(ir.Assign)
        for stmt in assigns:
            if isinstance(stmt.value, ir.FreeVar):
                idx = stmt.value.index
                assert(idx < len(args))
                stmt.value = args[idx]


def _replace_returns(blocks, target, return_label):
    """
    Return return statement by assigning directly to target, and a jump.
    """
    for label, block in blocks.items():
        casts = []
        for i in range(len(block.body)):
            stmt = block.body[i]
            if isinstance(stmt, ir.Return):
                assert(i + 1 == len(block.body))
                block.body[i] = ir.Assign(stmt.value, target, stmt.loc)
                block.body.append(ir.Jump(return_label, stmt.loc))
                # remove cast of the returned value
                for cast in casts:
                    if cast.target.name == stmt.value.name:
                        cast.value = cast.value.value
            elif isinstance(stmt, ir.Assign) and isinstance(stmt.value, ir.Expr) and stmt.value.op == 'cast':
                casts.append(stmt)

def _add_definitions(func_ir, block):
    """
    Add variable definitions found in a block to parent func_ir.
    """
    definitions = func_ir._definitions
    assigns = block.find_insts(ir.Assign)
    for stmt in assigns:
        definitions[stmt.target.name].append(stmt.value)

class GuardException(Exception):
    pass

def require(cond):
    """
    Raise GuardException if the given condition is False.
    """
    if not cond:
       raise GuardException

def guard(func, *args):
    """
    Run a function with given set of arguments, and guard against
    any GuardException raised by the function by returning None,
    or the expected return results if no such exception was raised.
    """
    try:
        return func(*args)
    except GuardException:
        return None

def _get_definition(func_ir, name, **kwargs):
    """
    Same as func_ir.get_definition(name), but raise GuardException if
    exception KeyError is caught.
    """
    try:
        return func_ir.get_definition(name, **kwargs)
    except KeyError:
        raise GuardException

def _find_arraycall(func_ir, block):
    """Look for statement like "x = numpy.array(y)" or "x[..] = y"
    immediately after the closure call that creates list y (the i-th
    statement in block).  Return the statement index if found, or
    raise GuardException.
    """
    array_var = None
    array_call_index = None
    list_var_dead_after_array_call = False
    list_var = None

    i = 0
    while i < len(block.body):
        instr = block.body[i]
        if isinstance(instr, ir.Del):
            # Stop the process if list_var becomes dead
            if list_var and array_var and instr.value == list_var.name:
                list_var_dead_after_array_call = True
                break
            pass
        elif isinstance(instr, ir.Assign):
            # Found array_var = array(list_var)
            lhs  = instr.target
            expr = instr.value
            if (guard(_find_numpy_call, func_ir, expr) == 'array' and
                isinstance(expr.args[0], ir.Var)):
                list_var = expr.args[0]
                array_var = lhs
                array_stmt_index = i
                array_kws = dict(expr.kws)
        elif (isinstance(instr, ir.SetItem) and
              isinstance(instr.value, ir.Var) and
              not list_var):
            list_var = instr.value
            # Found array_var[..] = list_var, the case for nested array
            array_var = instr.target
            array_def = _get_definition(func_ir, array_var)
            require(guard(_find_unsafe_empty_inferred, func_ir, array_def))
            array_stmt_index = i
            array_kws = {}
        else:
            # Bail out otherwise
            break
        i = i + 1
    # require array_var is found, and list_var is dead after array_call.
    require(array_var and list_var_dead_after_array_call)
    _make_debug_print("find_array_call")(block.body[array_stmt_index])
    return list_var, array_stmt_index, array_kws

def _find_numpy_call(func_ir, expr):
    """Check if a call expression is calling a numpy function, and
    return the callee's function name if it is, or raise GuardException.
    """
    require(isinstance(expr, ir.Expr) and expr.op == 'call')
    callee = expr.func
    callee_def = _get_definition(func_ir, callee)
    require(isinstance(callee_def, ir.Expr) and callee_def.op == 'getattr')
    module = callee_def.value
    module_def = _get_definition(func_ir, module)
    require(isinstance(module_def, ir.Global) and module_def.value == np)
    _make_debug_print("find_numpy_call")(callee_def.attr)
    return callee_def.attr

def _find_iter_range(func_ir, range_iter_var):
    """Find the iterator's actual range if it is either range(n), or range(m, n),
    otherwise return raise GuardException.
    """
    debug_print = _make_debug_print("find_iter_range")
    range_iter_def = _get_definition(func_ir, range_iter_var)
    debug_print("range_iter_var = ", range_iter_var, " def = ", range_iter_def)
    require(isinstance(range_iter_def, ir.Expr) and range_iter_def.op == 'getiter')
    range_var = range_iter_def.value
    range_def = _get_definition(func_ir, range_var)
    debug_print("range_var = ", range_var, " range_def = ", range_def)
    require(isinstance(range_def, ir.Expr) and range_def.op == 'call')
    func_var = range_def.func
    func_def = _get_definition(func_ir, func_var)
    debug_print("func_var = ", func_var, " func_def = ", func_def)
    require(isinstance(func_def, ir.Global) and func_def.value == range)
    nargs = len(range_def.args)
    if nargs == 1:
        stop = _get_definition(func_ir, range_def.args[0], lhs_only=True)
        return (0, range_def.args[0], func_def)
    elif nargs == 2:
        start = _get_definition(func_ir, range_def.args[0], lhs_only=True)
        stop = _get_definition(func_ir, range_def.args[1], lhs_only=True)
        return (start, stop, func_def)
    else:
        raise GuardException

def _inline_arraycall(func_ir, cfg, visited, loop, enable_prange=False):
    """Look for array(list) call in the exit block of a given loop, and turn list operations into
    array operations in the loop if the following conditions are met:
      1. The exit block contains an array call on the list;
      2. The list variable is no longer live after array call;
      3. The list is created in the loop entry block;
      4. The loop is created from an range iterator whose length is known prior to the loop;
      5. There is only one list_append operation on the list variable in the loop body;
      6. The block that contains list_append dominates the loop head, which ensures list
         length is the same as loop length;
    If any condition check fails, no modification will be made to the incoming IR.
    """
    debug_print = _make_debug_print("inline_arraycall")
    # There should only be one loop exit
    require(len(loop.exits) == 1)
    exit_block = next(iter(loop.exits))
    list_var, array_call_index, array_kws = _find_arraycall(func_ir, func_ir.blocks[exit_block])

    # check if dtype is present in array call
    dtype_def = None
    dtype_mod_def = None
    if 'dtype' in array_kws:
        require(isinstance(array_kws['dtype'], ir.Var))
        # We require that dtype argument to be a constant of getattr Expr, and we'll
        # remember its definition for later use.
        dtype_def = _get_definition(func_ir, array_kws['dtype'])
        require(isinstance(dtype_def, ir.Expr) and dtype_def.op == 'getattr')
        dtype_mod_def = _get_definition(func_ir, dtype_def.value)

    list_var_def = _get_definition(func_ir, list_var)
    debug_print("list_var = ", list_var, " def = ", list_var_def)
    if isinstance(list_var_def, ir.Expr) and list_var_def.op == 'cast':
        list_var_def = _get_definition(func_ir, list_var_def.value)
    # Check if the definition is a build_list
    require(isinstance(list_var_def, ir.Expr) and list_var_def.op ==  'build_list')

    # Look for list_append in "last" block in loop body, which should be a block that is
    # a post-dominator of the loop header.
    list_append_stmts = []
    for label in loop.body:
        # We have to consider blocks of this loop, but not sub-loops.
        # To achieve this, we require the set of "in_loops" of "label" to be visited loops.
        in_visited_loops = [l.header in visited for l in cfg.in_loops(label)]
        if not all(in_visited_loops):
            continue
        block = func_ir.blocks[label]
        debug_print("check loop body block ", label)
        for stmt in block.find_insts(ir.Assign):
            lhs = stmt.target
            expr = stmt.value
            if isinstance(expr, ir.Expr) and expr.op == 'call':
                func_def = _get_definition(func_ir, expr.func)
                if isinstance(func_def, ir.Expr) and func_def.op == 'getattr' \
                  and func_def.attr == 'append':
                    list_def = _get_definition(func_ir, func_def.value)
                    debug_print("list_def = ", list_def, list_def == list_var_def)
                    if list_def == list_var_def:
                        # found matching append call
                        list_append_stmts.append((label, block, stmt))

    # Require only one list_append, otherwise we won't know the indices
    require(len(list_append_stmts) == 1)
    append_block_label, append_block, append_stmt = list_append_stmts[0]

    # Check if append_block (besides loop entry) dominates loop header.
    # Since CFG doesn't give us this info without loop entry, we approximate
    # by checking if the predecessor set of the header block is the same
    # as loop_entries plus append_block, which is certainly more restrictive
    # than necessary, and can be relaxed if needed.
    preds = set(l for l, b in cfg.predecessors(loop.header))
    debug_print("preds = ", preds, (loop.entries | set([append_block_label])))
    require(preds == (loop.entries | set([append_block_label])))

    # Find iterator in loop header
    iter_vars = []
    iter_first_vars = []
    loop_header = func_ir.blocks[loop.header]
    for stmt in loop_header.find_insts(ir.Assign):
        expr = stmt.value
        if isinstance(expr, ir.Expr):
            if expr.op == 'iternext':
                iter_def = _get_definition(func_ir, expr.value)
                debug_print("iter_def = ", iter_def)
                iter_vars.append(expr.value)
            elif expr.op == 'pair_first':
                iter_first_vars.append(stmt.target)

    # Require only one iterator in loop header
    require(len(iter_vars) == 1 and len(iter_first_vars) == 1)
    iter_var = iter_vars[0] # variable that holds the iterator object
    iter_first_var = iter_first_vars[0] # variable that holds the value out of iterator

    # Final requirement: only one loop entry, and we're going to modify it by:
    # 1. replacing the list definition with an array definition;
    # 2. adding a counter for the array iteration.
    require(len(loop.entries) == 1)
    loop_entry = func_ir.blocks[next(iter(loop.entries))]
    terminator = loop_entry.terminator
    scope = loop_entry.scope
    loc = loop_entry.loc
    stmts = []
    removed = []
    def is_removed(val, removed):
        if isinstance(val, ir.Var):
            for x in removed:
                if x.name == val.name:
                    return True
        return False
    # Skip list construction and skip terminator, add the rest to stmts
    for i in range(len(loop_entry.body) - 1):
        stmt = loop_entry.body[i]
        if isinstance(stmt, ir.Assign) and (stmt.value == list_def or is_removed(stmt.value, removed)):
            removed.append(stmt.target)
        else:
            stmts.append(stmt)
    debug_print("removed variables: ", removed)

    # Define an index_var to index the array.
    # If the range happens to be single step ranges like range(n), or range(m, n),
    # then the index_var correlates to iterator index; otherwise we'll have to
    # define a new counter.
    range_def = guard(_find_iter_range, func_ir, iter_var)
    index_var = scope.make_temp(loc)
    if range_def and range_def[0] == 0:
        # iterator starts with 0, index_var can just be iter_first_var
        index_var = iter_first_var
    else:
        # index_var = -1 # starting the index with -1 since it will incremented in loop header
        stmts.append(_new_definition(func_ir, index_var, ir.Const(value=-1, loc=loc), loc))

    # Insert statement to get the size of the loop iterator
    size_var = scope.make_temp(loc)
    if range_def:
        start, stop, range_func_def = range_def
        if start == 0:
            size_val = stop
        else:
            size_val = ir.Expr.binop(fn='-', lhs=stop, rhs=start, loc=loc)
        # we can parallelize this loop if enable_prange = True, by changing
        # range function from range, to prange.
        if enable_prange and isinstance(range_func_def, ir.Global):
            range_func_def.name = 'prange'
            range_func_def.value = prange

    else:
        len_func_var = scope.make_temp(loc)
        stmts.append(_new_definition(func_ir, len_func_var,
                     ir.Global('range_iter_len', range_iter_len, loc=loc), loc))
        size_val = ir.Expr.call(len_func_var, (iter_var,), (), loc=loc)

    stmts.append(_new_definition(func_ir, size_var, size_val, loc))

    size_tuple_var = scope.make_temp(loc)
    stmts.append(_new_definition(func_ir, size_tuple_var,
                 ir.Expr.build_tuple(items=[size_var], loc=loc), loc))

    array_var = scope.make_temp(loc)
    # Insert array allocation
    array_var = scope.make_temp(loc)
    empty_func = scope.make_temp(loc)
    if dtype_def and dtype_mod_def:
        # when dtype is present, we'll call emtpy with dtype
        dtype_mod_var = scope.make_temp(loc)
        dtype_var = scope.make_temp(loc)
        stmts.append(_new_definition(func_ir, dtype_mod_var, dtype_mod_def, loc))
        stmts.append(_new_definition(func_ir, dtype_var,
                         ir.Expr.getattr(dtype_mod_var, dtype_def.attr, loc), loc))
        stmts.append(_new_definition(func_ir, empty_func,
                         ir.Global('empty', np.empty, loc=loc), loc))
        array_kws = [('dtype', dtype_var)]
    else:
        # otherwise we'll call unsafe_empty_inferred
        stmts.append(_new_definition(func_ir, empty_func,
                         ir.Global('unsafe_empty_inferred',
                             unsafe_empty_inferred, loc=loc), loc))
        array_kws = []
    # array_var = empty_func(size_tuple_var)
    stmts.append(_new_definition(func_ir, array_var,
                 ir.Expr.call(empty_func, (size_tuple_var,), list(array_kws), loc=loc), loc))

    # Add back removed just in case they are used by something else
    for var in removed:
        stmts.append(_new_definition(func_ir, var, array_var, loc))

    # Add back terminator
    stmts.append(terminator)
    # Modify loop_entry
    loop_entry.body = stmts

    if range_def:
        if range_def[0] != 0:
            # when range doesn't start from 0, index_var becomes loop index
            # (iter_first_var) minus an offset (range_def[0])
            terminator = loop_header.terminator
            assert(isinstance(terminator, ir.Branch))
            # find the block in the loop body that header jumps to
            block_id = terminator.truebr
            blk = func_ir.blocks[block_id]
            loc = blk.loc
            blk.body.insert(0, _new_definition(func_ir, index_var,
                ir.Expr.binop(fn='-', lhs=iter_first_var,
                                      rhs=range_def[0], loc=loc),
                loc))
    else:
        # Insert index_var increment to the end of loop header
        loc = loop_header.loc
        terminator = loop_header.terminator
        stmts = loop_header.body[0:-1]
        next_index_var = scope.make_temp(loc)
        one = scope.make_temp(loc)
        # one = 1
        stmts.append(_new_definition(func_ir, one,
                     ir.Const(value=1,loc=loc), loc))
        # next_index_var = index_var + 1
        stmts.append(_new_definition(func_ir, next_index_var,
                     ir.Expr.binop(fn='+', lhs=index_var, rhs=one, loc=loc), loc))
        # index_var = next_index_var
        stmts.append(_new_definition(func_ir, index_var, next_index_var, loc))
        stmts.append(terminator)
        loop_header.body = stmts

    # In append_block, change list_append into array assign
    for i in range(len(append_block.body)):
        if append_block.body[i] == append_stmt:
            debug_print("Replace append with SetItem")
            append_block.body[i] = ir.SetItem(target=array_var, index=index_var,
                                              value=append_stmt.value.args[0], loc=append_stmt.loc)

    # replace array call, by changing "a = array(b)" to "a = b"
    stmt = func_ir.blocks[exit_block].body[array_call_index]
    # stmt can be either array call or SetItem, we only replace array call
    if isinstance(stmt, ir.Assign) and isinstance(stmt.value, ir.Expr):
        stmt.value = array_var
        func_ir._definitions[stmt.target.name] = [stmt.value]

    return True


def _find_unsafe_empty_inferred(func_ir, expr):
    unsafe_empty_inferred
    require(isinstance(expr, ir.Expr) and expr.op == 'call')
    callee = expr.func
    callee_def = _get_definition(func_ir, callee)
    require(isinstance(callee_def, ir.Global))
    _make_debug_print("_find_unsafe_empty_inferred")(callee_def.value)
    return callee_def.value == unsafe_empty_inferred


def _fix_nested_array(func_ir):
    """Look for assignment like: a[..] = b, where both a and b are numpy arrays, and
    try to eliminate array b by expanding a with an extra dimension.
    """
    """
    cfg = compute_cfg_from_blocks(func_ir.blocks)
    all_loops = list(cfg.loops().values())
    def find_nest_level(label):
        level = 0
        for loop in all_loops:
            if label in loop.body:
                level += 1
    """

    def find_array_def(arr):
        """Find numpy array definition such as
            arr = numba.unsafe.ndarray.empty_inferred(...).
        If it is arr = b[...], find array definition of b recursively.
        """
        arr_def = func_ir.get_definition(arr)
        _make_debug_print("find_array_def")(arr, arr_def)
        if isinstance(arr_def, ir.Expr):
            if guard(_find_unsafe_empty_inferred, func_ir, arr_def):
                return arr_def
            elif arr_def.op == 'getitem':
                return find_array_def(arr_def.value)
        raise GuardException

    def fix_array_assign(stmt):
        """For assignment like lhs[idx] = rhs, where both a and b are arrays, do the
        following:
        1. find the definition of rhs, which has to be a call to numba.unsafe.ndarray.empty_inferred
        2. find the source array creation for lhs, insert an extra dimension of size of b.
        3. replace the definition of rhs = numba.unsafe.ndarray.empty_inferred(...) with rhs = lhs[idx]
        """
        require(isinstance(stmt, ir.SetItem))
        require(isinstance(stmt.value, ir.Var))
        debug_print = _make_debug_print("fix_array_assign")
        debug_print("found SetItem: ", stmt)
        lhs = stmt.target
        # Find the source array creation of lhs
        lhs_def = find_array_def(lhs)
        debug_print("found lhs_def: ", lhs_def)
        rhs_def = _get_definition(func_ir, stmt.value)
        debug_print("found rhs_def: ", rhs_def)
        require(isinstance(rhs_def, ir.Expr))
        if rhs_def.op == 'cast':
            rhs_def = _get_definition(func_ir, rhs_def.value)
            require(isinstance(rhs_def, ir.Expr))
        require(_find_unsafe_empty_inferred(func_ir, rhs_def))
        # Find the array dimension of rhs
        dim_def = _get_definition(func_ir, rhs_def.args[0])
        require(isinstance(dim_def, ir.Expr) and dim_def.op == 'build_tuple')
        debug_print("dim_def = ", dim_def)
        extra_dims = [ _get_definition(func_ir, x, lhs_only=True) for x in dim_def.items ]
        debug_print("extra_dims = ", extra_dims)
        # Expand size tuple when creating lhs_def with extra_dims
        size_tuple_def = _get_definition(func_ir, lhs_def.args[0])
        require(isinstance(size_tuple_def, ir.Expr) and size_tuple_def.op == 'build_tuple')
        debug_print("size_tuple_def = ", size_tuple_def)
        size_tuple_def.items += extra_dims
        # In-place modify rhs_def to be getitem
        rhs_def.op = 'getitem'
        rhs_def.value = _get_definition(func_ir, lhs, lhs_only=True)
        rhs_def.index = stmt.index
        del rhs_def._kws['func']
        del rhs_def._kws['args']
        del rhs_def._kws['vararg']
        del rhs_def._kws['kws']
        # success
        return True

    for label in find_topo_order(func_ir.blocks):
        block = func_ir.blocks[label]
        for stmt in block.body:
            if guard(fix_array_assign, stmt):
                block.body.remove(stmt)

def _new_definition(func_ir, var, value, loc):
    func_ir._definitions[var.name] = [value]
    return ir.Assign(value=value, target=var, loc=loc)

