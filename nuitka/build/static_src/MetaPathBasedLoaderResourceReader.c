//     Copyright 2021, Kay Hayen, mailto:kay.hayen@gmail.com
//
//     Part of "Nuitka", an optimizing Python compiler that is compatible and
//     integrates with CPython, but also works on its own.
//
//     Licensed under the Apache License, Version 2.0 (the "License");
//     you may not use this file except in compliance with the License.
//     You may obtain a copy of the License at
//
//        http://www.apache.org/licenses/LICENSE-2.0
//
//     Unless required by applicable law or agreed to in writing, software
//     distributed under the License is distributed on an "AS IS" BASIS,
//     WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
//     See the License for the specific language governing permissions and
//     limitations under the License.
//
// This implements the resource reader for of C compiled modules and
// shared library extension modules bundled for standalone mode with
// newer Python.

// This file is included from another C file, help IDEs to still parse it on
// its own.
#ifdef __IDE_ONLY__
#include "nuitka/prelude.h"
#include "nuitka/unfreezing.h"
#endif

// Just for the IDE to know, this file is not included otherwise.
#if PYTHON_VERSION >= 0x370

struct Nuitka_ResourceReaderObject {
    /* Python object folklore: */
    PyObject_HEAD

        /* The loader entry, to know this is about exactly. */
        struct Nuitka_MetaPathBasedLoaderEntry const *m_loader_entry;
};

static void Nuitka_ResourceReader_tp_dealloc(struct Nuitka_ResourceReaderObject *reader) {
    Nuitka_GC_UnTrack(reader);

    PyObject_GC_Del(reader);
}

static PyObject *Nuitka_ResourceReader_tp_repr(struct Nuitka_ResourceReaderObject *reader) {
    return PyUnicode_FromFormat("<nuitka_resource_reader for '%s'>", reader->m_loader_entry->name);
}

static int Nuitka_ResourceReader_tp_traverse(struct Nuitka_ResourceReaderObject *reader, visitproc visit, void *arg) {
    return 0;
}

static PyObject *_Nuitka_ResourceReader_resource_path(struct Nuitka_ResourceReaderObject *reader, PyObject *resource) {
    PyObject *dir_name = getModuleDirectory(reader->m_loader_entry);

    if (unlikely(dir_name == NULL)) {
        return NULL;
    }

    PyObject *result = JOIN_PATH2(dir_name, resource);
    Py_DECREF(dir_name);

    return result;
}

static PyObject *Nuitka_ResourceReader_resource_path(struct Nuitka_ResourceReaderObject *reader, PyObject *args,
                                                     PyObject *kwds) {
    PyObject *resource;

    int res = PyArg_ParseTupleAndKeywords(args, kwds, "O:resource_path", (char **)_kwlist_get_data, &resource);

    if (unlikely(res == 0)) {
        return NULL;
    }

    return _Nuitka_ResourceReader_resource_path(reader, resource);
}

static PyObject *Nuitka_ResourceReader_open_resource(struct Nuitka_ResourceReaderObject *reader, PyObject *args,
                                                     PyObject *kwds) {
    PyObject *resource;

    int res = PyArg_ParseTupleAndKeywords(args, kwds, "O:open_resource", (char **)_kwlist_get_data, &resource);

    if (unlikely(res == 0)) {
        return NULL;
    }

    PyObject *filename = _Nuitka_ResourceReader_resource_path(reader, resource);

    return BUILTIN_OPEN_SIMPLE(filename, "rb", false);
}

static PyMethodDef Nuitka_ResourceReader_methods[] = {
    {"resource_path", (PyCFunction)Nuitka_ResourceReader_resource_path, METH_VARARGS | METH_KEYWORDS, NULL},
    {"open_resource", (PyCFunction)Nuitka_ResourceReader_open_resource, METH_VARARGS | METH_KEYWORDS, NULL},

    {NULL}};

static PyGetSetDef Nuitka_ResourceReader_getsetlist[] = {{NULL}};

static PyTypeObject Nuitka_ResourceReader_Type = {
    PyVarObject_HEAD_INIT(NULL, 0) "nuitka_resource_reader",
    sizeof(struct Nuitka_ResourceReaderObject),      /* tp_basicsize */
    0,                                               /* tp_itemsize */
    (destructor)Nuitka_ResourceReader_tp_dealloc,    /* tp_dealloc */
    0,                                               /* tp_print */
    0,                                               /* tp_getattr */
    0,                                               /* tp_setattr */
    0,                                               /* tp_reserved */
    (reprfunc)Nuitka_ResourceReader_tp_repr,         /* tp_repr */
    0,                                               /* tp_as_number */
    0,                                               /* tp_as_sequence */
    0,                                               /* tp_as_mapping */
    0,                                               /* tp_hash */
    0,                                               /* tp_call */
    0,                                               /* tp_str */
    PyObject_GenericGetAttr,                         /* tp_getattro */
    0,                                               /* tp_setattro */
    0,                                               /* tp_as_buffer */
    Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,         /* tp_flags */
    0,                                               /* tp_doc */
    (traverseproc)Nuitka_ResourceReader_tp_traverse, /* tp_traverse */
    0,                                               /* tp_clear */
    0,                                               /* tp_richcompare */
    0,                                               /* tp_weaklistoffset */
    0,                                               /* tp_iter */
    0,                                               /* tp_iternext */
    Nuitka_ResourceReader_methods,                   /* tp_methods */
    0,                                               /* tp_members */
    Nuitka_ResourceReader_getsetlist,                /* tp_getset */
};

static PyObject *Nuitka_ResourceReader_New(struct Nuitka_MetaPathBasedLoaderEntry const *entry) {
    struct Nuitka_ResourceReaderObject *result;

    result = (struct Nuitka_ResourceReaderObject *)PyObject_GC_New(struct Nuitka_ResourceReaderObject,
                                                                   &Nuitka_ResourceReader_Type);
    Nuitka_GC_Track(result);

    result->m_loader_entry = entry;

    return (PyObject *)result;
}

#endif