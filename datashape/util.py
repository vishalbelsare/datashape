# -*- coding: utf-8 -*-
from __future__ import print_function, division, absolute_import

import inspect
import operator
import itertools
import ctypes
import collections
import string
import sys
from functools import partial

try:
    from collections import MutableMapping
except ImportError as e:
    # Python 3
    from UserDict import DictMixin as MutableMapping

from . import py2help
from . import parser
from . import type_symbol_table
from .error import UnificationError
from .validation import validate
from .coretypes import (DataShape, Fixed, TypeVar, Record, Ellipsis, String,
               uint8, uint16, uint32, uint64, CType, Mono, JSON,
               int8, int16, int32, int64,
               float32, float64, complex64, complex128,
               Type, free, type_constructor)
from .typesets import TypeSet


__all__ = ['dshape', 'dshapes', 'cat_dshapes',
           'dummy_signature', 'verify',
           'from_ctypes', 'from_cffi', 'to_ctypes', 'from_llvm',
           'to_numba', 'from_numba', 'gensym']


PY3 = (sys.version_info[:2] >= (3,0))

# Legacy from blaze.compute.llvm_array. Putting here to just depend on llvmpy
SCALAR = 0
POINTER = 1

#------------------------------------------------------------------------
# Utility Functions for DataShapes
#------------------------------------------------------------------------

def dshapes(*args):
    """
    Parse a bunch of datashapes all at once.

    >>> a, b = dshapes('3 * int32', '2 * var * float64')
    """
    return [dshape(arg) for arg in args]

def dshape(o):
    """
    Parse a blaze type. For a thorough description see
    http://blaze.pydata.org/docs/datashape.html

    """
    ds = _dshape(o)
    validate(ds)
    return ds


def _dshape(o):
    if isinstance(o, py2help._strtypes):
        return parser.parse(o, type_symbol_table.sym)
    elif isinstance(o, (CType, String, Record, JSON)):
        return DataShape(o)
    elif isinstance(o, Mono):
        return o
    else:
        raise TypeError('Cannot create dshape from object of type %s' % type(o))


def cat_dshapes(dslist):
    """
    Concatenates a list of dshapes together along
    the first axis. Raises an error if there is
    a mismatch along another axis or the measures
    are different.

    Requires that the leading dimension be a known
    size for all data shapes.
    TODO: Relax this restriction to support
          streaming dimensions.
    """
    if len(dslist) == 0:
        raise ValueError('Cannot concatenate an empty list of dshapes')
    elif len(dslist) == 1:
        return dslist[0]

    outer_dim_size = operator.index(dslist[0][0])
    inner_ds = dslist[0][1:]
    for ds in dslist[1:]:
        outer_dim_size += operator.index(ds[0])
        if ds[1:] != inner_ds:
            raise ValueError(('The datashapes to concatenate much'
                            ' all match after'
                            ' the first dimension (%s vs %s)') %
                            (inner_ds, ds[1:]))
    return DataShape(*[Fixed(outer_dim_size)] + list(inner_ds))


def dummy_signature(f):
    """Create a dummy signature for `f`"""
    from . import coretypes as T
    argspec = inspect.getargspec(f)
    n = len(argspec.args)
    return T.Function(*[T.TypeVar(gensym()) for i in range(n + 1)])


def verify(t1, t2):
    """Verify that two immediate type constructors are valid for unification"""
    if not isinstance(t1, Mono) or not isinstance(t2, Mono):
        if t1 != t2:
            raise UnificationError("%s != %s" % (t1, t2))
        return

    args1, args2 = t1.parameters, t2.parameters
    tcon1, tcon2 = type_constructor(t1), type_constructor(t2)

    if tcon1 != tcon2:
        raise UnificationError(
            "Got differing type constructors %s and %s" % (tcon1, tcon2))

    if len(args1) != len(args2):
        raise UnificationError("%s got %d and %d arguments" % (
            tcon1, len(args1), len(args2)))


#------------------------------------------------------------------------
# DataShape Conversion
#------------------------------------------------------------------------
def _from_cffi_internal(ffi, ctype):
    k = ctype.kind
    if k == 'struct':
        # TODO: Assuming the field offsets match
        #       blaze kernels - need to sync up blaze, dynd,
        #       cffi, numpy, etc so that the field offsets always work!
        #       Also need to make sure there are no bitsize/bitshift
        #       values that would be incompatible.
        return Record([(f[0], _from_cffi_internal(ffi, f[1].type))
                        for f in ctype.fields])
    elif k == 'array':
        if ctype.length is None:
            # Only the first array can have the size
            # unspecified, so only need a single name
            dsparams = [TypeVar('N')]
        else:
            dsparams = [Fixed(ctype.length)]
        ctype = ctype.item
        while ctype.kind == 'array':
            dsparams.append(Fixed(ctype.length))
            ctype = ctype.item
        dsparams.append(_from_cffi_internal(ffi, ctype))
        return DataShape(*dsparams)
    elif k == 'primitive':
        cn = ctype.cname
        if cn in ['signed char', 'short', 'int',
                        'long', 'long long']:
            so = ffi.sizeof(ctype)
            if so == 1:
                return int8
            elif so == 2:
                return int16
            elif so == 4:
                return int32
            elif so == 8:
                return int64
            else:
                raise TypeError('cffi primitive "%s" has invalid size %d' %
                                (cn, so))
        elif cn in ['unsigned char', 'unsigned short',
                        'unsigned int', 'unsigned long',
                        'unsigned long long']:
            so = ffi.sizeof(ctype)
            if so == 1:
                return uint8
            elif so == 2:
                return uint16
            elif so == 4:
                return uint32
            elif so == 8:
                return uint64
            else:
                raise TypeError('cffi primitive "%s" has invalid size %d' %
                                (cn, so))
        elif cn == 'float':
            return float32
        elif cn == 'double':
            return float64
        else:
            raise TypeError('Unrecognized cffi primitive "%s"' % cn)
    elif k == 'pointer':
        raise TypeError('a pointer can only be at the outer level of a cffi type '
                        'when converting to blaze datashape')
    else:
        raise TypeError('Unrecognized cffi kind "%s"' % k)


def from_cffi(ffi, ctype):
    """
    Constructs a blaze dshape from a cffi type.
    """
    # Allow one pointer dereference at the outermost level
    if ctype.kind == 'pointer':
        ctype = ctype.item
    return _from_cffi_internal(ffi, ctype)

def to_ctypes(dshape):
    """
    Constructs a ctypes type from a datashape
    """
    if len(dshape) == 1:
        if dshape == int8:
            return ctypes.c_int8
        elif dshape == int16:
            return ctypes.c_int16
        elif dshape == int32:
            return ctypes.c_int32
        elif dshape == int64:
            return ctypes.c_int64
        elif dshape == uint8:
            return ctypes.c_uint8
        elif dshape == uint16:
            return ctypes.c_uint16
        elif dshape == uint32:
            return ctypes.c_uint32
        elif dshape == uint64:
            return ctypes.c_uint64
        elif dshape == float32:
            return ctypes.c_float
        elif dshape == float64:
            return ctypes.c_double
        elif dshape == complex64:
            class Complex64(ctypes.Structure):
                _fields_ = [('real', ctypes.c_float),
                            ('imag', ctypes.c_float)]
                _blaze_type_ = complex64
            return Complex64
        elif dshape == complex128:
            class Complex128(ctypes.Structure):
                _fields_ = [('real', ctypes.c_double),
                            ('imag', ctypes.c_double)]
                _blaze_type_ = complex128
            return Complex128
        elif isinstance(dshape, Record):
            fields = [(name, to_ctypes(dshape.fields[name]))
                                          for name in dshape.names]
            class temp(ctypes.Structure):
                _fields_ = fields
            return temp
        else:
            raise TypeError("Cannot convert datashape %r into ctype" % dshape)
    # Create arrays
    else:
        if isinstance(dshape[0], (TypeVar, Ellipsis)):
            num = 0
        else:
            num = int(dshape[0])
        return num*to_ctypes(dshape.subarray(1))


# FIXME: Add a field
def from_ctypes(ctype):
    """
    Constructs a blaze dshape from a ctypes type.
    """
    if issubclass(ctype, ctypes.Structure):
        fields = []
        if hasattr(ctype, '_blaze_type_'):
            return ctype._blaze_type_
        for nm, tp in ctype._fields_:
            child_ds = from_ctypes(tp)
            fields.append((nm, child_ds))
        ds = Record(fields)
        # TODO: Validate that the ctypes offsets match
        #       the C offsets blaze uses
        return ds
    elif issubclass(ctype, ctypes.Array):
        dstup = []
        while issubclass(ctype, ctypes.Array):
            dstup.append(Fixed(ctype._length_))
            ctype = ctype._type_
        dstup.append(from_ctypes(ctype))
        return DataShape(*dstup)
    elif ctype == ctypes.c_int8:
        return int8
    elif ctype == ctypes.c_int16:
        return int16
    elif ctype == ctypes.c_int32:
        return int32
    elif ctype == ctypes.c_int64:
        return int64
    elif ctype == ctypes.c_uint8:
        return uint8
    elif ctype == ctypes.c_uint16:
        return uint16
    elif ctype == ctypes.c_uint32:
        return uint32
    elif ctype == ctypes.c_uint64:
        return uint64
    elif ctype == ctypes.c_float:
        return float32
    elif ctype == ctypes.c_double:
        return float64
    else:
        raise TypeError('Cannot convert ctypes %r into '
                        'a blaze datashape' % ctype)

# Class to hold Pointer temporarily
def _PointerDshape(object):
    def __init__(self, dshape):
        self.dshape = dshape


def from_llvm(typ, argkind=SCALAR):
    """
    Map an LLVM type to an equivalent datashape type

    argkind is SCALAR, POINTER, or a tuple of (arrkind, nd, el_type) for Arrays
    """
    from llvm_array import check_array
    import llvm.core

    kind = typ.kind
    if argkind is None and kind == llvm.core.TYPE_POINTER:
        argkind = check_array(typ.pointee)
        if argkind is None:
            argkind = POINTER
    if kind == llvm.core.TYPE_INTEGER:
        ds = dshape("int" + str(typ.width))

    elif kind == llvm.core.TYPE_DOUBLE:
        ds = float64

    elif kind == llvm.core.TYPE_FLOAT:
        ds = float32

    elif kind == llvm.core.TYPE_VOID:
        ds = None

    elif kind == llvm.core.TYPE_POINTER:
        ds = ''
        pointee = typ.pointee
        p_kind = pointee.kind
        if p_kind == llvm.core.TYPE_INTEGER:
            width = pointee.width
            # Special case:  char * is mapped to strings
            if width == 8:
                ds = dshape("string")
            else:
                ds = _PointerDshape(from_llvm(pointee))
        if p_kind == llvm.core.TYPE_STRUCT:
            if argkind == POINTER:
                ds = _PointerDshape(from_llvm(pointee))
            else:  # argkind is a tuple of (arrkind, nd, pointer_type)
                nd = argkind[1]
                eltype = from_llvm(argkind[2])
                obj = [TypeVar('i'+str(n)) for n in range(nd)]
                obj.append(eltype)
                ds = DataShape(*obj)
                ds._array_kind = argkind[0]

    elif kind == llvm.core.TYPE_STRUCT:
        if not typ.is_literal:
            struct_name = typ.name.split('.')[-1]
            if not PY3:
                struct_name = struct_name.encode('ascii')
        else:
            struct_name = ''

        names = [ "e"+str(n) for n in range(typ.element_count) ]

        fields = [(name, from_llvm(elem))
                   for name, elem in zip(names, typ.elements)]
        typstr = "{ %s }" % ("; ".join(["{0}: {1}".format(*field)
                                            for field in fields]))

        ds = dshape(typstr)
    else:
        raise TypeError("Unknown type %s" % kind)
    return ds


# FIXME: This is a hack
def from_numba(nty):
    return Type._registry[str(nty)]


# Just scalars for now
# FIXME: This could be improved
def to_numba(ds):
    import numba
    # Fixup the complex type to how numba does it
    s = str(ds)
    s = {'complex[float32]':'complex64', 'complex[float64]':'complex128', 'bool':'bool_'}.get(s, s)
    return getattr(numba, s)


#------------------------------------------------------------------------
# Temporary names
#------------------------------------------------------------------------

def make_temper():
    """Return a function that returns temporary names"""
    temps = collections.defaultdict(int)

    def temper(name=""):
        varname = name.rstrip(string.digits)
        count = temps[varname]
        temps[varname] += 1
        if varname and count == 0:
            return varname
        return varname + str(count)

    return temper

def make_stream(seq, _temp=make_temper()):
    """Create a stream of temporaries seeded by seq"""
    while 1:
        for x in seq:
            yield _temp(x)

gensym = partial(next, make_stream(string.ascii_uppercase))
