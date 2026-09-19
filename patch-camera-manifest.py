#!/usr/bin/env python3
"""Add <uses-native-library> declarations to OnePlusCamera.apk's binary manifest.

Why: the stock camera APK targets SDK>=31 but predates the targetSdk>=31 rule
that hides vendor public libraries from app linker namespaces unless they are
declared with <uses-native-library>. Without the declaration libnativeloader
does not create the app->sphal link for libcdsprpc.so, SNPE's
isRuntimeAvailable(DSP) returns false, and portrait-video bokeh silently
falls back to ~180 ms/frame CPU inference. Declaring the libs restores CDSP
inference with no framework patches.

The script is idempotent: patching an already-patched APK is a no-op. A
pristine backup is kept as <apk>.orig next to the APK on first run.

usage: patch-camera-manifest.py [path/to/OnePlusCamera.apk]
       (default: vendor/oneplus/camera/proprietary/product/priv-app/
        OnePlusCamera/OnePlusCamera.apk resolved from this file's location)
"""
import os
import shutil
import struct
import sys
import zipfile

LIBS = ["libcdsprpc.so", "libadsprpc.so", "libqti-perfd-client.so"]
ANDROID_NS_URL = "http://schemas.android.com/apk/res/android"
A_NAME_ID = 0x01010003          # android:name   (informational)
A_REQUIRED_ID = 0x0101028e      # android:required


def die(msg):
    sys.stderr.write("patch-camera-manifest: %s\n" % msg)
    sys.exit(1)


class Axml:
    def __init__(self, data):
        self.d = bytearray(data)
        t, h, sz = struct.unpack_from("<HHI", self.d, 0)
        if t != 0x0003:
            die("not a binary AXML")
        P = 8
        t, h, psz = struct.unpack_from("<HHI", self.d, P)
        if t != 0x0001:
            die("no string pool")
        sc, sty, fl, ss, ys = struct.unpack_from("<IIIII", self.d, P + 8)
        if fl & 0x100:
            die("utf-8 string pool not supported")
        if sty:
            die("styled pools not supported")
        self.P, self.psz, self.sc, self.ss = P, psz, sc, ss
        self.off = [struct.unpack_from("<I", self.d, P + 28 + i * 4)[0] for i in range(sc)]
        self.names = []
        base = P + ss
        for o in self.off:
            n = struct.unpack_from("<H", self.d, base + o)[0]
            self.names.append(self.decode(base + o + 2, n))
        q = P + psz
        self.name_idx = self.str_idx("name")
        self.ns_url_idx = self.str_idx(ANDROID_NS_URL)
        self.resmap = None
        while q < len(self.d):
            t, h, sz = struct.unpack_from("<HHI", self.d, q)
            if t == 0x0180:
                self.resmap = [struct.unpack_from("<I", self.d, q + 8 + i * 4)[0]
                               for i in range((sz - 8) // 4)]
            if t == 0x0102:
                ns, nm = struct.unpack_from("<II", self.d, q + 16)
                if nm != 0xFFFFFFFF and self.names[nm] == "application":
                    self.app_off = q
                    self.app_node = struct.unpack_from("<IIIIHHHHI", self.d, q + 16)
                    break
            q += sz
        else:
            die("<application> element not found")

    def decode(self, pos, n):
        return bytes(self.d[pos:pos + n * 2]).decode("utf-16-le")

    def str_idx(self, s):
        try:
            return self.names.index(s)
        except ValueError:
            return None

    def add_strings(self, strs):
        P = self.P
        data0 = P + self.ss
        data = bytearray(self.d[data0:P + self.psz])
        offs = list(self.off)
        idxs = []
        for s in strs:
            offs.append(len(data))
            idxs.append(len(offs) - 1)
            data += struct.pack("<H", len(s)) + s.encode("utf-16-le") + b"\x00\x00"
        while (28 + 4 * len(offs) + len(data)) % 4:
            data += b"\x00\x00"
        blob = struct.pack("<HHI", 0x0001, 28, 28 + 4 * len(offs) + len(data))
        blob += struct.pack("<IIIII", len(offs), 0, 0, 28 + 4 * len(offs), 0)
        blob += b"".join(struct.pack("<I", o) for o in offs) + data
        shift = len(blob) - self.psz
        self.d = self.d[:P] + blob + self.d[P + self.psz:]
        # The root header's total size must account for the pool growth too.
        old_root = struct.unpack_from("<I", self.d, 4)[0]
        struct.pack_into("<I", self.d, 4, old_root + shift)
        self.psz = struct.unpack_from("<I", self.d, P + 4)[0]
        self.sc = len(offs)
        self.app_off += shift
        return idxs

    def elem_bytes(self, name_idx, lib_idx, req_idx):
        line = struct.unpack_from("<I", self.d, self.app_off + 8)[0]
        asz = self.app_node[7] or 20  # attrSize from application node
        aext = 20
        nattr = 2 if req_idx is not None else 1
        start_sz = 16 + aext + nattr * asz
        node = struct.pack("<HHII", 0x0102, 16, start_sz, line) + struct.pack("<I", 0xFFFFFFFF)
        ext = struct.pack("<IIHHHHI", 0xFFFFFFFF, name_idx, 0x14, asz, nattr, 0, 0)
        a0 = struct.pack("<III", self.ns_url_idx, self.name_idx, lib_idx) + struct.pack("<HBBi", 8, 0, 0x03, lib_idx)
        a1 = b""
        if req_idx is not None:
            a1 = (struct.pack("<III", self.ns_url_idx, req_idx, 0xFFFFFFFF)
                  + struct.pack("<HBBi", 8, 0, 0x12, 0))
        end = struct.pack("<HHII", 0x0103, 16, 24, line) + struct.pack("<I", 0xFFFFFFFF) + struct.pack("<II", 0xFFFFFFFF, name_idx)
        assert len(node + ext + a0 + a1) == start_sz
        return node + ext + a0 + a1 + end

    def insert_elements(self, blocks):
        ins = b"".join(blocks)
        p = self.app_off + struct.unpack_from("<I", self.d, self.app_off + 4)[0]
        self.d = self.d[:p] + ins + self.d[p:]
        old = struct.unpack_from("<I", self.d, 4)[0]
        struct.pack_into("<I", self.d, 4, old + len(ins))

    def verify(self):
        q = self.P + self.psz
        depth = 0
        while q < len(self.d):
            t, h, sz = struct.unpack_from("<HHI", self.d, q)
            if sz == 0 or q + sz > len(self.d):
                die("self-verify: broken chunk at %d" % q)
            if t == 0x0102:
                depth += 1
            elif t == 0x0103:
                depth -= 1
            q += sz
        if q != len(self.d) or depth != 0:
            die("self-verify: unbalanced tree")


def main():
    if len(sys.argv) > 1:
        apk = sys.argv[1]
    else:
        here = os.path.dirname(os.path.abspath(__file__))
        apk = os.path.normpath(os.path.join(
            here, "../../../vendor/oneplus/camera/proprietary/product/priv-app/"
                 "OnePlusCamera/OnePlusCamera.apk"))
    if not os.path.exists(apk):
        die("no such apk: " + apk)

    with zipfile.ZipFile(apk) as z:
        man = z.read("AndroidManifest.xml")
        names = [i.filename for i in z.infolist()]

    ax = Axml(man)
    if ax.str_idx("uses-native-library") is not None:
        print("already patched, skipping:", apk)
        return

    name_idx = ax.str_idx("name")
    req_idx = ax.str_idx("required")
    ns_url = ax.str_idx(ANDROID_NS_URL)
    if None in (name_idx, ns_url):
        die("required strings absent")
    if req_idx is None:
        print("note: 'required' string absent -> declaring libs without it (default true)")
    if ax.resmap is not None and (name_idx >= len(ax.resmap) or (req_idx is not None and req_idx >= len(ax.resmap))):
        die("attribute-name indices beyond resource map; refusing")

    idxs = ax.add_strings(["uses-native-library"] + LIBS)
    el_idx = idxs[0]
    lib_idxs = idxs[1:]
    blocks = []
    for li in lib_idxs:
        blocks.append(ax.elem_bytes(el_idx, li, req_idx))
    ax.insert_elements(blocks)
    ax.verify()

    patched = bytes(ax.d)
    backup = apk + ".orig"
    if not os.path.exists(backup):
        shutil.copy2(apk, backup)
        print("backup kept:", backup)
    tmp = apk + ".tmp"
    with zipfile.ZipFile(apk) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            if item.filename.startswith("META-INF/") and item.filename.endswith(
                    (".RSA", ".SF", ".MF", ".EC", ".DSA")):
                continue
            data = patched if item.filename == "AndroidManifest.xml" else zin.read(item.filename)
            zi = zipfile.ZipInfo(item.filename, date_time=item.date_time)
            zi.compress_type = item.compress_type
            zi.external_attr = item.external_attr
            zout.writestr(zi, data)
    os.replace(tmp, apk)
    print("patched %s: declared %s" % (os.path.basename(apk), ", ".join(LIBS)))


if __name__ == "__main__":
    main()
