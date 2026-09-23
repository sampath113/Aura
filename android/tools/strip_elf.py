#!/usr/bin/env python3
"""Remove debug information from an ELF64 shared library, without touching a
single byte that the dynamic loader reads.

Why this exists
---------------
The llama.cpp Android release publishes *unstripped* binaries. The fourteen
files AURA bundles weigh 231.6 MB, of which 216 MB is DWARF debug info and
symbol tables that no phone ever looks at. Dropping it takes the engine to
about 27 MB, and the APK from roughly 250 MB to roughly 45 MB - which is the
difference between an install people give up on and one that just works.

The obvious tool for the job is `strip`, but the build machines this project
uses only have the host's GNU binutils, which is built for x86-64 and cannot
read an aarch64 ELF at all:

    /usr/bin/strip: Unable to recognise the format of the input file libllama.so

Installing a cross-binutils or a 700 MB NDK to save 200 MB of download is a bad
trade, so this file does the one thing that is needed - and only that thing -
with the Python standard library.

What "only that thing" means
----------------------------
The ELF format keeps two views of the same file: *program headers*, which say
what to map into memory at run time, and *section headers*, which say what each
region means to a linker, a debugger or a profiler. A running program - and
Android's linker in particular - only ever reads the program header view.

So:

  * bytes [0, end of the last PT_LOAD segment) are copied across **verbatim**.
    That is the ELF header, every program header, .dynamic, .dynsym, .dynstr,
    .hash, .rela.*, .text, .rodata - everything that is ever mapped or read;
  * sections that are not SHF_ALLOC (or reachable from one that is) are
    dropped: the .debug_* family, .symtab, .strtab, .comment, .ARM.attributes;
  * surviving sections that lived *after* the loadable part (.shstrtab) are
    copied to a fresh place, and the section header table is rewritten to match
    - kept sections keep their index, so every sh_link and sh_info stays valid,
    and dropped ones become SHT_NULL entries, which every tool skips.

A file that does not fit that shape is refused rather than guessed at: 32-bit or
big-endian, extended section numbering, a debug section sitting *inside* a
loadable segment, or an SHF_ALLOC section outside every segment. A refused file
is left exactly as it was - the engine does not need its symbols, so keeping
them is a bigger APK, not a broken one.

The result is checked before it replaces anything: every loadable segment must
be byte-identical to the original, the ELF header must be unchanged apart from
the section table offset, and the rewritten file must parse cleanly with no
section running off its end.

Usage
-----
    strip_elf.py [--strip-unneeded] FILE [FILE ...]

(The flag is accepted and ignored so this can stand in for a real `strip`: the
Gradle build probes a candidate tool by running it exactly that way.)

Exit status is 0 when the input was understood - whether or not it shrank - and
3 when no input could be read at all, which is how the build tells "this tool
does not work here" apart from "this tool worked, there was nothing to do".
"""

import collections
import os
import struct
import sys

ELF_MAGIC = b"\x7fELF"
ELFCLASS64 = 2
ELFDATA2LSB = 1
PT_LOAD = 1
SHF_ALLOC = 0x2
SHT_NULL = 0
SHT_REL = 9
SHT_RELA = 4
SHT_NOBITS = 8
SHN_XINDEX = 0xFFFF
RELOCATION_TYPES = (SHT_REL, SHT_RELA)
NOTHING_TO_STRIP = "nothing to strip"

EHDR = struct.Struct("<16sHHIQQQIHHHHHH")
PHDR = struct.Struct("<IIQQQQQQ")
SHDR = struct.Struct("<IIQQQQIIQQ")

Segment = collections.namedtuple("Segment", "type flags offset vaddr paddr filesz memsz align")
Section = collections.namedtuple("Section", "name type flags addr offset size link info addralign entsize")


class Unsupported(Exception):
    """This file is not shaped the way this tool is allowed to rewrite."""


def human(count):
    value = float(count)
    unit = "GB"
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            break
        value /= 1024
    digits = 0 if unit in ("B", "KB") else 1
    return ("%%.%df %%s" % digits) % (value, unit)


def cstring(blob, offset):
    if offset >= len(blob):
        raise Unsupported("a section name points past the string table")
    end = blob.find(b"\0", offset)
    if end < 0:
        raise Unsupported("a section name is not terminated")
    return blob[offset:end].decode("utf-8", "replace")


class Elf:
    """The two header tables this tool reasons about, and the section names."""

    def __init__(self, data):
        if len(data) < EHDR.size or data[:4] != ELF_MAGIC:
            raise Unsupported("not an ELF file")
        fields = EHDR.unpack_from(data, 0)
        ident = fields[0]
        if ident[4] != ELFCLASS64:
            raise Unsupported("not a 64-bit ELF")
        if ident[5] != ELFDATA2LSB:
            raise Unsupported("not little-endian")
        (_, self.type, self.machine, _, self.entry, self.phoff, self.shoff, _,
         _, self.phentsize, self.phnum, self.shentsize, self.shnum, self.shstrndx) = fields
        if self.shnum == 0 or self.shstrndx >= SHN_XINDEX:
            raise Unsupported("extended section numbering")
        if self.phnum == 0 or self.phoff == 0:
            raise Unsupported("no program headers")
        if self.phentsize != PHDR.size or self.shentsize != SHDR.size:
            raise Unsupported("unexpected header sizes")
        for start, count, size, what in ((self.phoff, self.phnum, self.phentsize, "program"),
                                         (self.shoff, self.shnum, self.shentsize, "section")):
            if start + count * size > len(data):
                raise Unsupported("the %s header table runs off the end" % what)
        self.data = data
        self.segments = [Segment(*PHDR.unpack_from(data, self.phoff + i * self.phentsize))
                         for i in range(self.phnum)]
        self.sections = [Section(*SHDR.unpack_from(data, self.shoff + i * self.shentsize))
                         for i in range(self.shnum)]
        strings = self.sections[self.shstrndx]
        blob = data[strings.offset:strings.offset + strings.size]
        self.names = [cstring(blob, section.name) for section in self.sections]

    @property
    def loads(self):
        return [segment for segment in self.segments if segment.type == PT_LOAD]


def plan(elf):
    """Decide what survives, and check that the file really does fit the shape.

    Returns (prefix_end, keep, drop). Everything before `prefix_end` is copied
    verbatim; `keep` are the section indices that still describe something, and
    `drop` the ones that become SHT_NULL holes.
    """
    loads = elf.loads
    if not loads:
        raise Unsupported("no loadable segments")
    prefix_end = max(segment.offset + segment.filesz for segment in loads)
    if elf.shoff < prefix_end:
        raise Unsupported("the section header table sits inside the loadable part")

    keep = set()
    for index, section in enumerate(elf.sections):
        if section.type == SHT_NULL:
            continue
        if section.flags & SHF_ALLOC or index == elf.shstrndx:
            keep.add(index)
    # A section that a kept section points at (sh_link, and sh_info for the
    # relocation sections) has to stay, or the reference dangles.
    growing = True
    while growing:
        growing = False
        for index in sorted(keep):
            section = elf.sections[index]
            targets = [section.link]
            if section.type in RELOCATION_TYPES:
                targets.append(section.info)
            for target in targets:
                if 0 < target < len(elf.sections) and target not in keep:
                    keep.add(target)
                    growing = True

    drop = set()
    for index, section in enumerate(elf.sections):
        if index in keep or section.type in (SHT_NULL, SHT_NOBITS) or section.size == 0:
            continue
        if section.offset < prefix_end:
            raise Unsupported("%s keeps its data inside a loadable segment" % elf.names[index])
        drop.add(index)
    if not drop:
        raise Unsupported(NOTHING_TO_STRIP)

    # Every surviving byte has to be somewhere the loader can still find it:
    # inside a segment, or after all of them (where it can be moved freely).
    for index in sorted(keep):
        section = elf.sections[index]
        if section.type in (SHT_NULL, SHT_NOBITS) or section.size == 0:
            continue
        start, end = section.offset, section.offset + section.size
        if start < prefix_end < end:
            raise Unsupported("%s straddles the loadable part" % elf.names[index])
        if end <= prefix_end and not any(segment.offset <= start and end <= segment.offset + segment.filesz
                                         for segment in loads):
            raise Unsupported("%s is not inside any loadable segment" % elf.names[index])
    return prefix_end, keep, drop


def verify(before, after, drop):
    """Refuse to hand back anything that changed a byte the loader reads.

    `e_shoff` is the single exception, and the reason this check is written out
    rather than being a plain slice comparison: the section header table moved,
    and the field that records where it lives sits in the ELF header, which is
    itself inside the first PT_LOAD segment. No loader ever reads it - only
    linkers and debuggers do - so it is allowed to differ, and it must differ
    by exactly the new offset.
    """
    if len(after) >= len(before.data):
        raise Unsupported("the file did not get smaller")
    shoff_field = 0x28
    new_shoff = struct.unpack_from("<Q", after, shoff_field)[0]
    for segment in before.loads:
        start, end = segment.offset, segment.offset + segment.filesz
        wanted = bytearray(before.data[start:end])
        offset_in_segment = shoff_field - start
        if 0 <= offset_in_segment and offset_in_segment + 8 <= len(wanted):
            struct.pack_into("<Q", wanted, offset_in_segment, new_shoff)
        if after[start:end] != bytes(wanted):
            raise Unsupported("a loadable segment changed")
    old = EHDR.unpack_from(before.data, 0)
    new = EHDR.unpack_from(after, 0)
    if old[:6] + old[7:] != new[:6] + new[7:]:
        raise Unsupported("the ELF header changed beyond the section table offset")

    check = Elf(after)
    if check.names[check.shstrndx] != ".shstrtab":
        raise Unsupported("the section name table did not survive")
    for index, section in enumerate(check.sections):
        if section.type in (SHT_NULL, SHT_NOBITS) or section.size == 0:
            continue
        if section.offset + section.size > len(after):
            raise Unsupported("%s runs off the end of the new file" % check.names[index])
    for index, name in enumerate(check.names):
        if index in drop:
            continue
        if name != before.names[index]:
            raise Unsupported("section %d changed its name" % index)
        if name.startswith(".debug"):
            raise Unsupported("%s survived" % name)


def stripped_image(elf):
    prefix_end, keep, drop = plan(elf)
    image = bytearray(elf.data[:prefix_end])

    moved = {}
    for index in sorted(keep):
        section = elf.sections[index]
        if section.type == SHT_NOBITS or section.size == 0:
            continue
        if section.offset + section.size <= prefix_end:
            continue
        while len(image) % max(1, section.addralign):
            image.append(0)
        moved[index] = len(image)
        image += elf.data[section.offset:section.offset + section.size]

    while len(image) % 8:
        image.append(0)
    shoff = len(image)
    for index, section in enumerate(elf.sections):
        if index in drop:
            # An empty slot rather than a removal: every kept section keeps its
            # index, so no sh_link or sh_info has to be rewritten.
            image += SHDR.pack(0, SHT_NULL, 0, 0, 0, 0, 0, 0, 0, 0)
        else:
            image += SHDR.pack(section.name, section.type, section.flags, section.addr,
                               moved.get(index, section.offset), section.size,
                               section.link, section.info, section.addralign, section.entsize)

    header = list(EHDR.unpack_from(bytes(image), 0))
    header[6] = shoff
    EHDR.pack_into(image, 0, *header)

    after = bytes(image)
    verify(elf, after, drop)
    return after, len(drop)


def strip_file(path):
    """Strip one file in place. Returns (old, new, dropped) sizes and count."""
    with open(path, "rb") as handle:
        data = handle.read()
    before = Elf(data)
    image, dropped = stripped_image(before)
    temporary = path + ".stripping"
    with open(temporary, "wb") as handle:
        handle.write(image)
    os.chmod(temporary, os.stat(path).st_mode)
    os.replace(temporary, path)
    return len(data), len(image), dropped


def main(argv):
    paths = [argument for argument in argv if not argument.startswith("-")]
    if not paths:
        sys.stderr.write("usage: strip_elf.py [--strip-unneeded] FILE [FILE ...]\n")
        return 2
    stripped = 0
    nothing = 0
    for path in paths:
        try:
            old, new, dropped = strip_file(path)
        except Unsupported as reason:
            if str(reason) == NOTHING_TO_STRIP:
                nothing += 1
                print("%s: already stripped" % os.path.basename(path))
            else:
                sys.stderr.write("%s: left alone - %s\n" % (path, reason))
            continue
        except OSError as error:
            sys.stderr.write("%s: left alone - %s\n" % (path, error))
            continue
        stripped += 1
        print("%s: %s -> %s (dropped %d debug sections)"
              % (os.path.basename(path), human(old), human(new), dropped))
    if not stripped and not nothing:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
