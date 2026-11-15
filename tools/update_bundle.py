"""Yama guncellemesi icin yayin dosyalarini uretir (CI'da calisir).

Kullanim:
    python tools/update_bundle.py <surum> "<dist\\Video Downloader>" <cikti_klasoru>

Uretilenler:
  * <app_dir>/update_manifest.json
        Kurulumun icine girer. Uygulama "bende hangi dosyalar, hangi hash'le
        var" sorusunu bununla cevaplar; her guncellemede tum kurulumu
        yeniden hash'lemek zorunda kalmaz.
  * <cikti>/update-manifest.json   (ayni icerik, Release'e yuklenir)
  * <cikti>/app-files.zip          (Release'e yuklenir)
        Kurulumun TUM dosyalari, her biri ayri ayri sikistirilmis. Uygulama
        bu zip'i bastan sona indirmez: once sondaki dizini (central
        directory), sonra sadece degisen dosyalarin bayt araliklarini HTTP
        Range ile ceker. Yani 80 MB'lik pakette 3 dosya degistiyse sadece o
        3 dosya iner.

Manifest bicimi:
    {"version": "1.3.9",
     "files": {"_internal/python312.dll": {"sha256": "...", "size": 123}, ...}}
Yollar her zaman "/" ile yazilir.
"""

import hashlib
import json
import os
import sys
import zipfile

MANIFEST_NAME = "update_manifest.json"


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def collect(app_dir):
    files = {}
    for root, dirs, names in os.walk(app_dir):
        dirs.sort()
        for name in sorted(names):
            full = os.path.join(root, name)
            rel = os.path.relpath(full, app_dir).replace(os.sep, "/")
            if rel == MANIFEST_NAME:
                continue
            files[rel] = {"sha256": sha256_file(full),
                          "size": os.path.getsize(full)}
    return files


def main():
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    version, app_dir, out_dir = sys.argv[1:]
    os.makedirs(out_dir, exist_ok=True)

    files = collect(app_dir)
    if not files:
        sys.exit("bos klasor: %s" % app_dir)
    manifest = {"version": version, "files": files}
    data = json.dumps(manifest, indent=1, sort_keys=True).encode("utf-8")

    with open(os.path.join(app_dir, MANIFEST_NAME), "wb") as f:
        f.write(data)
    with open(os.path.join(out_dir, "update-manifest.json"), "wb") as f:
        f.write(data)

    # Zip'e manifest DAHIL edilmez; uygulama manifesti ayri dosyadan alir ve
    # kurulumdaki kopyayi guncelleme bitince kendisi yazar.
    zpath = os.path.join(out_dir, "app-files.zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel in sorted(files):
            z.write(os.path.join(app_dir, rel.replace("/", os.sep)), rel)

    total = sum(v["size"] for v in files.values())
    print("manifest: %d dosya, %.1f MB; zip: %.1f MB"
          % (len(files), total / 1e6, os.path.getsize(zpath) / 1e6))


if __name__ == "__main__":
    main()
