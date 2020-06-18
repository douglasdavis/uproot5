# BSD 3-Clause License; see https://github.com/scikit-hep/uproot4/blob/master/LICENSE

from __future__ import absolute_import

import struct

import numpy

import uproot4.const
import uproot4.model
import uproot4._util


scope = {
    "struct": struct,
    "numpy": numpy,
    "VersionedModel": uproot4.model.VersionedModel,
}


def _actually_compile(class_code, new_scope):
    exec(compile(class_code, "<dynamic>", "exec"), new_scope)


def compile_class(file, classes, class_code, class_name):
    new_scope = dict(scope)
    for cls in classes.values():
        new_scope[cls.__name__] = cls

    def c(name, version=None):
        cls = new_scope.get(uproot4.model.classname_encode(name, version))
        if cls is None:
            cls = new_scope.get(uproot4.model.classname_encode(name))
        if cls is None:
            cls = file.class_named(name, version)
        return cls

    new_scope["c"] = c

    _actually_compile(class_code, new_scope)

    out = new_scope[class_name]
    out.class_code = class_code
    out.__module__ = "<dynamic>"

    behavior_cls = uproot4.behavior_of(uproot4.model.classname_decode(class_name)[0])
    if behavior_cls is not None:
        out = uproot4._util.new_class(out.__name__, (behavior_cls, out), {})
        out.__module__ = "<dynamic>"

    return out


class DeserializationError(Exception):
    __slots__ = ["message", "context", "file_path"]

    def __init__(self, message, context, file_path):
        self.message = message
        self.context = context
        self.file_path = file_path

    def __str__(self):
        lines = []
        indent = "    "
        for obj in self.context.get("breadcrumbs", ()):
            lines.append(
                "{0}{1} version {2} as {3}.{4}".format(
                    indent,
                    obj.classname,
                    obj.instance_version,
                    type(obj).__module__,
                    type(obj).__name__,
                )
            )
            indent = indent + "    "
            for v in getattr(obj, "_bases", []):
                lines.append("{0}(base): {1}".format(indent, repr(v)))
            for k, v in getattr(obj, "_members", {}).items():
                lines.append("{0}{1}: {2}".format(indent, k, repr(v)))

        in_parent = ""
        if "TBranch" in self.context:
            in_parent = "\nin TBranch {0}".format(self.context["TBranch"].object_path)
        elif "TKey" in self.context:
            in_parent = "\nin object {0}".format(self.context["TKey"].object_path)

        if len(lines) == 0:
            return """{0}
in file {1}{2}""".format(
                self.message, self.file_path, in_parent
            )
        else:
            return """while reading

{0}

{1}
in file {2}{3}""".format(
                "\n".join(lines), self.message, self.file_path, in_parent
            )


_numbytes_version_1 = struct.Struct(">IH")
_numbytes_version_2 = struct.Struct(">H")


def numbytes_version(chunk, cursor, context, move=True):
    num_bytes, version = cursor.fields(chunk, _numbytes_version_1, context, move=False)
    num_bytes = numpy.int64(num_bytes)

    if num_bytes & uproot4.const.kByteCountMask:
        num_bytes = int(num_bytes & ~uproot4.const.kByteCountMask) + 4
        if move:
            cursor.skip(_numbytes_version_1.size)

    else:
        num_bytes = None
        version = cursor.field(chunk, _numbytes_version_2, context, move=move)

    return num_bytes, version


def numbytes_check(start_cursor, stop_cursor, num_bytes, classname, context, file_path):
    if num_bytes is not None:
        observed = stop_cursor.displacement(start_cursor)
        if observed != num_bytes:
            raise uproot4.deserialization.DeserializationError(
                """expected {0} bytes but cursor moved by {1} bytes (through {2})""".format(
                    num_bytes, observed, classname
                ),
                context,
                file_path,
            )


_map_string_string_format1 = struct.Struct(">I")


def map_string_string(chunk, cursor, context):
    cursor.skip(12)
    size = cursor.field(chunk, _map_string_string_format1, context)
    cursor.skip(6)
    keys = [cursor.string(chunk, context) for i in range(size)]
    cursor.skip(6)
    values = [cursor.string(chunk, context) for i in range(size)]
    return dict(zip(keys, values))


scope["map_string_string"] = map_string_string


_read_object_any_format1 = struct.Struct(">I")


def read_object_any(chunk, cursor, context, file, parent, as_class=None):
    # TBufferFile::ReadObjectAny()
    # https://github.com/root-project/root/blob/c4aa801d24d0b1eeb6c1623fd18160ef2397ee54/io/io/src/TBufferFile.cxx#L2684
    # https://github.com/root-project/root/blob/c4aa801d24d0b1eeb6c1623fd18160ef2397ee54/io/io/src/TBufferFile.cxx#L2404

    beg = cursor.displacement()
    bcnt = numpy.int64(cursor.field(chunk, _read_object_any_format1, context))

    if (bcnt & uproot4.const.kByteCountMask) == 0 or (
        bcnt == uproot4.const.kNewClassTag
    ):
        vers = 0
        start = 0
        tag = bcnt
        bcnt = 0
    else:
        vers = 1
        start = cursor.displacement()
        tag = numpy.int64(cursor.field(chunk, _read_object_any_format1, context))
        bcnt = int(bcnt)

    if tag & uproot4.const.kClassMask == 0:
        # reference object

        if tag == 0:
            return None  # return null

        elif tag == 1:
            return parent  # return parent

        elif tag not in cursor.refs:
            # jump past this object
            cursor.move_to(cursor.origin + beg + bcnt + 4)
            return None  # return null

        else:
            return cursor.refs[int(tag)]  # return object

    elif tag == uproot4.const.kNewClassTag:
        # new class and object

        classname = cursor.classname(chunk, context)

        cls = file.class_named(classname)

        if vers > 0:
            cursor.refs[start + uproot4.const.kMapOffset] = cls
        else:
            cursor.refs[len(cursor.refs) + 1] = cls

        if as_class is None:
            obj = cls.read(chunk, cursor, context, file, parent)
        else:
            obj = as_class.read(chunk, cursor, context, file, parent)

        if vers > 0:
            cursor.refs[beg + uproot4.const.kMapOffset] = obj
        else:
            cursor.refs[len(cursor.refs) + 1] = obj

        return obj  # return object

    else:
        # reference class, new object

        ref = int(tag & ~uproot4.const.kClassMask)

        if as_class is None:
            if ref not in cursor.refs:
                if getattr(file, "file_path") is None:
                    in_file = ""
                else:
                    in_file = "\nin file {0}".format(file.file_path)
                raise OSError("""invalid class-tag reference{0}""".format(in_file))

            cls = cursor.refs[ref]  # reference class
            obj = cls.read(chunk, cursor, context, file, parent)

        else:
            obj = as_class.read(chunk, cursor, context, file, parent)

        if vers > 0:
            cursor.refs[beg + uproot4.const.kMapOffset] = obj
        else:
            cursor.refs[len(cursor.refs) + 1] = obj

        return obj  # return object


scope["read_object_any"] = read_object_any