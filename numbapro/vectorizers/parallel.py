"""
This file implements the code-generator for parallel-vectorize.

ParallelUFunc is the platform independent base class for generating
the thread dispatcher.  This thread dispatcher launches threads
that execute the generated function of UFuncCore.
UFuncCore is subclassed to specialize for the input/output types.
The actual workload is invoked inside the function generated by UFuncCore.
UFuncCore also defines a work-stealing mechanism that allows idle threads
to steal works from other threads.
"""
from __future__ import print_function, absolute_import
import multiprocessing
import numpy as np
import llvm.core as lc
import llvm.ee as le
from numba.npyufunc import ufuncbuilder
from numba import types

NUM_CPU = max(1, multiprocessing.cpu_count())


class ParallelUFuncBuilder(ufuncbuilder.UFuncBuilder):
    def build(self, cres):
        # Buider wrapper for ufunc entry point
        ctx = cres.target_context
        signature = cres.signature
        wrapper = build_ufunc_wrapper(ctx, cres.llvm_func, signature)
        ctx.engine.add_module(wrapper.module)
        ptr = ctx.engine.get_pointer_to_function(wrapper)
        # Get dtypes
        dtypenums = [np.dtype(a.name).num for a in signature.args]
        dtypenums.append(np.dtype(signature.return_type.name).num)
        return dtypenums, ptr


def build_ufunc_wrapper(ctx, lfunc, signature):
    innerfunc = ufuncbuilder.build_ufunc_wrapper(ctx, lfunc, signature)
    lfunc = build_ufunc_kernel(ctx, innerfunc, signature)
    ctx.optimize(lfunc.module)
    return lfunc


def build_ufunc_kernel(ctx, innerfunc, sig):

    """
    char **args, npy_intp *dimensions, npy_intp* steps, void* data
    """
    byte_t = lc.Type.int(8)
    byte_ptr_t = lc.Type.pointer(byte_t)

    intp_t = ctx.get_value_type(types.intp)

    fnty = lc.Type.function(lc.Type.void(), innerfunc.type.pointee.args)
    mod = innerfunc.module
    lfunc = mod.add_function(fnty, name=".kernel")

    bb_entry = lfunc.append_basic_block('')

    builder = lc.Builder.new(bb_entry)

    args, dimensions, steps, data = lfunc.args

    total = builder.load(dimensions)
    ncpu = lc.Constant.int(total.type, NUM_CPU)

    count = builder.udiv(total, ncpu)

    count_list = []
    remain = total

    for i in range(NUM_CPU):
        space = builder.alloca(intp_t)
        count_list.append(space)

        if i == NUM_CPU - 1:
            builder.store(remain, space)
        else:
            builder.store(count, space)
            remain = builder.sub(remain, count)

    array_count = len(sig.args) + 1

    steps_list = []
    for i in range(array_count):
        ptr = builder.gep(steps, [lc.Constant.int(lc.Type.int(), i)])
        step = builder.load(ptr)
        steps_list.append(step)

    args_list = []
    for i in range(NUM_CPU):
        space = builder.alloca(byte_ptr_t,
                               size=lc.Constant.int(lc.Type.int(), array_count))
        args_list.append(space)

        for j in range(array_count):
            dst = builder.gep(space, [lc.Constant.int(lc.Type.int(), j)])
            src = builder.gep(args, [lc.Constant.int(lc.Type.int(), j)])

            baseptr = builder.load(src)
            base = builder.ptrtoint(baseptr, intp_t)
            multiplier = lc.Constant.int(count.type, i)
            offset = builder.mul(steps_list[j], builder.mul(count, multiplier))
            addr = builder.inttoptr(builder.add(base, offset), baseptr.type)

            builder.store(addr, dst)

    add_task_ty = lc.Type.function(lc.Type.void(), [byte_ptr_t] * 5)
    empty_fnty = lc.Type.function(lc.Type.void(), ())
    add_task = mod.get_or_insert_function(add_task_ty, name='numba_add_task')
    synchronize = mod.get_or_insert_function(empty_fnty,
                                             name='numba_synchronize')
    ready = mod.get_or_insert_function(empty_fnty, name='numba_ready')

    as_void_ptr = lambda arg: builder.bitcast(arg, byte_ptr_t)

    for each_args, each_dims in zip(args_list, count_list):
        # builder.call(innerfunc, [each_args, each_dims, steps, data])
        innerargs = [as_void_ptr(x)
                     for x
                     in [innerfunc, each_args, each_dims, steps, data]]

        builder.call(add_task, innerargs)

    builder.call(ready, ())
    builder.call(synchronize, ())

    builder.ret_void()

    return lfunc


class _ProtectEngineDestroy(object):
    def __init__(self, set_cas, engine):
        self.set_cas = set_cas
        self.engine = engine

    def __del__(self):
        self.set_cas(0)


_keepalive = []


def _make_cas_function():
    mod = lc.Module.new("generate-cas")
    llint = lc.Type.int()
    llintp = lc.Type.pointer(llint)
    fnty = lc.Type.function(llint, [llintp, llint, llint])
    fn = mod.add_function(fnty, name='cas')
    ptr, old, repl = fn.args
    bb = fn.append_basic_block('')
    builder = lc.Builder.new(bb)
    out = builder.atomic_cmpxchg(ptr, old, repl, ordering='monotonic')
    builder.ret(out)

    mod.verify()

    engine = le.EngineBuilder.new(mod).opt(3).create()
    ptr = engine.get_pointer_to_function(fn)

    _keepalive.append(engine)
    return engine, ptr


def _init():
    from . import workqueue as lib
    from ctypes import CFUNCTYPE, c_int, c_void_p

    le.dylib_add_symbol('numba_new_thread', lib.new_thread_fnptr)
    le.dylib_add_symbol('numba_join_thread', lib.join_thread_fnptr)
    le.dylib_add_symbol('numba_add_task', lib.add_task)
    le.dylib_add_symbol('numba_synchronize', lib.synchronize)
    le.dylib_add_symbol('numba_ready', lib.ready)

    launch_threads = CFUNCTYPE(None, c_int)(lib.launch_threads)
    set_cas = CFUNCTYPE(None, c_void_p)(lib.set_cas)

    engine, cas_ptr = _make_cas_function()
    set_cas(c_void_p(cas_ptr))
    launch_threads(NUM_CPU)

    _keepalive.append(_ProtectEngineDestroy(set_cas, engine))



_init()