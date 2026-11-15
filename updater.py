"""
Video Downloader - Otomatik guncelleme

Iki ayri guncelleme kanali var:

1. yt-dlp kanali (sik, ~3 MB)
   YouTube/TikTok ayda birkac kez cikarim yontemini degistiriyor ve eski
   yt-dlp "HTTP Error 403" ya da "Unexpected response from webpage request"
   vermeye basliyor. Bu yuzden yt-dlp exe'nin icine GOMULMEZ; yazilabilir bir
   klasorden yuklenir ve oradan guncellenir. Boylece uygulamayi yeniden
   derlemeye gerek kalmaz.

2. Uygulama kanali
   GitHub Releases'teki son surum kontrol edilir. Sadece degisen dosyalar
   indirilir (yama); bkz. "Uygulama guncelleme" bolumu.

DIKKAT: activate() `import yt_dlp` YAPILMADAN ONCE cagrilmalidir. app.py bunu
kendi ilk satirlarinda yapar.
"""

import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
import zlib

APP_VERSION = "1.3.0"

GITHUB_REPO = "Viox-Development/video-downloader"
PYPI_YTDLP_JSON = "https://pypi.org/pypi/yt-dlp/json"
PYPI_EJS_JSON = "https://pypi.org/pypi/yt-dlp-ejs/%s/json"
GITHUB_LATEST_RELEASE = "https://api.github.com/repos/%s/releases/latest" % GITHUB_REPO

USER_AGENT = "VideoDownloader/%s" % APP_VERSION
NET_TIMEOUT = 10          # tek bir istegin azami suresi
CHECK_INTERVAL = 24 * 3600      # yt-dlp: gunde bir kez kontrol yeter
APP_CHECK_INTERVAL = 6 * 3600   # uygulama: gunde birkac kez bakmak yeterli

# Inno Setup sessiz kurulum bayraklari
SILENT_FLAGS = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                "/NOCANCEL", "/CLOSEAPPLICATIONS"]

# Ayni surumu bu kadar denedikten sonra otomatik kurulumdan vazgec; aksi
# halde kurulum her acilista basarisiz olup sonsuz donguye girebilir.
MAX_INSTALL_ATTEMPTS = 2


# --------------------------------------------------------------------------
# Klasorler
# --------------------------------------------------------------------------

def user_data_dir():
    """Yazilabilir uygulama klasoru (%LOCALAPPDATA%\\Video Downloader)."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Video Downloader")


def user_runtime_dir():
    """Guncellenebilir yt_dlp'nin durdugu klasor."""
    return os.path.join(user_data_dir(), "runtime")


def bundled_runtime_dir():
    """Kurulumla gelen yedek yt_dlp kopyasi (sadece frozen modda)."""
    if getattr(sys, "frozen", False):
        return os.path.join(sys._MEIPASS, "runtime")
    return None


def _state_path():
    return os.path.join(user_data_dir(), "update_state.json")


def _read_state():
    try:
        with open(_state_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_state(state):
    try:
        os.makedirs(user_data_dir(), exist_ok=True)
        with open(_state_path(), "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception:
        pass


# --------------------------------------------------------------------------
# yt-dlp'yi yukleme
# --------------------------------------------------------------------------

def _looks_like_ytdlp(path):
    """Klasorun ise yarar bir yt_dlp kopyasi oldugunu kabaca dogrula."""
    need = [
        os.path.join(path, "__init__.py"),
        os.path.join(path, "YoutubeDL.py"),
        os.path.join(path, "version.py"),
        os.path.join(path, "extractor", "__init__.py"),
    ]
    return all(os.path.isfile(p) for p in need)


def activate():
    """yt_dlp'yi once kullanici klasorunden, olmazsa paketten yukle.

    Bozuk bir indirme uygulamayi kilitlemesin diye kullanici kopyasi
    import edilemezse kenara alinir ve paketteki saglam kopyaya donulur.
    """
    candidates = []
    u = user_runtime_dir()
    if _looks_like_ytdlp(os.path.join(u, "yt_dlp")):
        candidates.append(u)
    b = bundled_runtime_dir()
    if b and _looks_like_ytdlp(os.path.join(b, "yt_dlp")):
        candidates.append(b)

    # sys.path'e ters sirada ekle ki ilk aday en onde kalsin
    for d in reversed(candidates):
        while d in sys.path:
            sys.path.remove(d)
        sys.path.insert(0, d)

    try:
        import yt_dlp  # noqa: F401
        return yt_dlp
    except Exception as first_err:
        # Kullanici kopyasi denenmediyse yapacak bir sey yok
        if not candidates or candidates[0] != u:
            raise

        # Kullanici kopyasini gecici olarak devreden cikar ve paketteki ile dene
        while u in sys.path:
            sys.path.remove(u)
        for mod in [m for m in sys.modules
                    if m == "yt_dlp" or m.startswith("yt_dlp.")]:
            sys.modules.pop(mod, None)

        try:
            import yt_dlp  # noqa: F401
        except Exception:
            # Paketteki kopya da ayni hatayi veriyor: sorun kullanici
            # kopyasinda DEGIL (or. paketten eksik bir modul). Saglam bir
            # kopyayi karantinaya almayalim; asil hatayi bildir.
            raise first_err

        # Buraya gelindiyse: paketteki calisiyor, kullanicininki gercekten
        # bozuk. Kenara al ve bir sonraki acilista yeniden indirilsin.
        try:
            bad = os.path.join(u, "yt_dlp")
            shutil.rmtree(bad + ".broken", ignore_errors=True)
            os.rename(bad, bad + ".broken")
        except Exception:
            pass
        flag_ytdlp_stale()
        return yt_dlp


def _version_from_dir(pkg_dir):
    """yt_dlp/version.py icindeki __version__'u dosyadan oku."""
    try:
        with open(os.path.join(pkg_dir, "version.py"), "r", encoding="utf-8") as f:
            m = re.search(r"^__version__\s*=\s*['\"]([^'\"]+)", f.read(), re.M)
        return m.group(1) if m else None
    except Exception:
        return None


def installed_ytdlp_version():
    """Yuklu (ya da yuklenecek) yt-dlp surumu.

    DIKKAT: acilista bu, yt_dlp import edilmeden ONCE cagrilir. Sadece
    `import yt_dlp` denemek o asamada None dondurur; o zaman surum
    karsilastirmasi atlanir ve guncel olsa bile her gun bosuna ~9,5 MB
    indirilir. Bu yuzden import yoksa surum dogrudan dosyadan okunur --
    activate() ile ayni oncelik sirasiyla.
    """
    mod = sys.modules.get("yt_dlp")
    if mod is not None:
        try:
            return mod.version.__version__
        except Exception:
            pass

    for d in (user_runtime_dir(), bundled_runtime_dir()):
        if not d:
            continue
        pkg = os.path.join(d, "yt_dlp")
        if _looks_like_ytdlp(pkg):
            v = _version_from_dir(pkg)
            if v:
                return v

    # Gelistirme modu: site-packages'taki kopya
    try:
        import yt_dlp
        return yt_dlp.version.__version__
    except Exception:
        return None


# --------------------------------------------------------------------------
# Ag yardimcilari
# --------------------------------------------------------------------------

def _get(url, timeout=NET_TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _get_json(url, timeout=NET_TIMEOUT):
    return json.loads(_get(url, timeout).decode("utf-8"))


def _version_tuple(v):
    """'2026.08.19' -> (2026, 8, 19); karsilastirilabilir hale getirir."""
    return tuple(int(x) for x in re.findall(r"\d+", v or ""))


# --------------------------------------------------------------------------
# yt-dlp guncelleme
# --------------------------------------------------------------------------

def latest_ytdlp():
    """PyPI'dan (surum, wheel_url) dondurur."""
    data = _get_json(PYPI_YTDLP_JSON)
    version = data["info"]["version"]
    for u in data["urls"]:
        if u.get("packagetype") == "bdist_wheel":
            return version, u["url"]
    raise RuntimeError("yt-dlp wheel bulunamadi")


def update_ytdlp(force=False):
    """Yeni yt-dlp varsa indirip kullanici klasorune kurar, ardindan
    yt-dlp-ejs'i yt-dlp'nin istedigi surume getirir.

    Dondurur: (guncellendi_mi, mesaj). Guncelleme bir sonraki ACILISTA
    devreye girer -- yt_dlp bu surecte zaten import edilmis durumda.
    """
    changed, message = _update_ytdlp_core(force)
    try:
        ejs_changed, ejs_message = ensure_ejs()
    except Exception as e:
        ejs_changed, ejs_message = False, "ejs: %s" % e
    if ejs_changed:
        message = "%s; %s" % (message, ejs_message)
    return changed or ejs_changed, message


def _update_ytdlp_core(force=False):
    state = _read_state()
    now = time.time()
    # Ilk calistirmada state dosyasi yoktur -> kontrol calisir.
    # Bozuk kopya karantinaya alindiginda activate() flag_ytdlp_stale() ile
    # bu zaman damgasini sifirlar -> yine kontrol calisir.
    if not force and now - state.get("ytdlp_checked_at", 0) < CHECK_INTERVAL:
        return False, "yakin zamanda kontrol edildi"

    current = installed_ytdlp_version()
    try:
        version, wheel_url = latest_ytdlp()
    except Exception as e:
        return False, "surum kontrolu basarisiz: %s" % e

    state["ytdlp_checked_at"] = now
    _write_state(state)

    if current and _version_tuple(version) <= _version_tuple(current):
        return False, "yt-dlp zaten guncel (%s)" % current

    runtime = user_runtime_dir()
    os.makedirs(runtime, exist_ok=True)
    staging = os.path.join(runtime, ".staging")
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging, exist_ok=True)

    try:
        blob = _get(wheel_url, timeout=120)
        whl = os.path.join(staging, "ytdlp.whl")
        with open(whl, "wb") as f:
            f.write(blob)
        with zipfile.ZipFile(whl) as z:
            z.extractall(staging)

        new_pkg = os.path.join(staging, "yt_dlp")
        if not _looks_like_ytdlp(new_pkg):
            raise RuntimeError("indirilen paket eksik gorunuyor")

        # Yerine koy: once eskisini kenara al, sonra yenisini tasi
        target = os.path.join(runtime, "yt_dlp")
        old = target + ".old"
        shutil.rmtree(old, ignore_errors=True)
        if os.path.isdir(target):
            os.rename(target, old)
        shutil.move(new_pkg, target)
        shutil.rmtree(old, ignore_errors=True)

        state["ytdlp_version"] = version
        _write_state(state)
        return True, "yt-dlp %s -> %s" % (current or "yok", version)
    except Exception as e:
        return False, "yt-dlp guncellenemedi: %s" % e
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def flag_ytdlp_stale():
    """Cikarim hatasi alindiginda cagrilir: sonraki aciliSta zorla kontrol et."""
    state = _read_state()
    state["ytdlp_checked_at"] = 0
    state["ytdlp_stale"] = True
    _write_state(state)


# --------------------------------------------------------------------------
# Uygulama guncelleme (GitHub Releases)
#
# Iki yol var:
#
# 1. YAMA (varsayilan). Her yayinda iki ek dosya bulunur:
#      update-manifest.json : kurulumdaki her dosyanin yolu, boyutu, SHA256'si
#      app-files.zip        : ayni dosyalar, her biri ayri sikistirilmis
#    Kurulumun icinde de bir update_manifest.json durur. Uygulama ikisini
#    karsilastirir ve zip'ten SADECE degisen dosyalari HTTP Range ile ceker
#    (once zip'in sonundaki dizini, sonra her dosyanin kendi bayt araligini).
#    Tipik bir kod guncellemesinde 80 MB yerine birkac MB iner; Inno Setup
#    hic calismaz, dosyalar robocopy ile yerine konur, birkac saniye surer.
#
# 2. TAM KURULUM (yedek). Yayinda yama dosyalari yoksa, Range desteklenmezse
#    ya da yama iki kez uygulanamadiysa eski yol: Kurulum.exe indirilir ve
#    sessiz kurulur.
# --------------------------------------------------------------------------

MANIFEST_NAME = "update_manifest.json"
BUNDLE_ASSET = "app-files.zip"
MANIFEST_ASSET = "update-manifest.json"

# Range isteklerini birlestirme esigi: arka arkaya duran iki degisen dosya
# arasindaki bosluk bundan kucukse tek istekte cekilir (yt-dlp gibi yuzlerce
# kucuk dosya degistiginde yuzlerce istek atmamak icin).
RANGE_MERGE_GAP = 256 * 1024


def latest_app_release():
    """GitHub'daki son surumun bilgileri (sozluk)."""
    data = _get_json(GITHUB_LATEST_RELEASE)
    rel = {
        "version": (data.get("tag_name") or "").lstrip("vV"),
        "notes": data.get("body") or "",
        "url": None, "sha256_url": None,
        "manifest_url": None, "bundle_url": None, "bundle_size": 0,
    }
    for a in data.get("assets", []):
        name = (a.get("name") or "").lower()
        link = a.get("browser_download_url")
        if name.endswith(".exe") and rel["url"] is None:
            rel["url"] = link
        elif "sha256" in name and rel["sha256_url"] is None:
            rel["sha256_url"] = link
        elif name == MANIFEST_ASSET:
            rel["manifest_url"] = link
        elif name == BUNDLE_ASSET:
            rel["bundle_url"] = link
            rel["bundle_size"] = int(a.get("size") or 0)
    return rel


def app_update_available():
    """Yeni uygulama surumu varsa bilgi sozlugu, yoksa None."""
    try:
        rel = latest_app_release()
    except Exception:
        return None
    if not rel["version"] or not (rel["url"] or rel["bundle_url"]):
        return None
    if _version_tuple(rel["version"]) <= _version_tuple(APP_VERSION):
        return None
    rel["current"] = APP_VERSION
    return rel


def _expected_sha256(sha256_url):
    """Yayindaki SHA256SUMS dosyasindan saglama toplamini oku."""
    text = _get(sha256_url, timeout=30).decode("utf-8", "replace")
    m = re.search(r"\b([0-9a-fA-F]{64})\b", text)
    return m.group(1).lower() if m else None


def download_app_installer(url, sha256_url=None, progress=None, dest=None):
    """Kurulum dosyasini indirip DOGRULAR, tam yolunu dondurur.

    Dogrulama neden sart: bu dosyayi uygulama kendisi indirip calistiriyor.
    urllib ile inen dosyaya Windows "Mark-of-the-Web" isareti konmadigi icin
    SmartScreen'in "bilinmeyen yayinci" onayi devreye girmez. (Defender'in
    gercek zamanli taramasi yine calisir, ama o kotu amacli yazilim arar;
    dosyanin BEKLENEN dosya olup olmadigini denetlemez.) Yani butunlugu
    dogrulayan tek yer burasi.

    Saglama toplami bulunamazsa guncelleme YAPILMAZ (dogrulanmamis bir exe
    calistirmaktansa guncellememek yegdir).

    Indirme once ".part" dosyasina yapilir: otomatik guncellemede yarim
    kalmis bir indirme "kurulmaya hazir surum" sanilmamali.
    """
    if not sha256_url:
        raise RuntimeError("yayinda SHA256 dosyasi yok, guncelleme durduruldu")

    expected = _expected_sha256(sha256_url)
    if not expected:
        raise RuntimeError("SHA256 dosyasi okunamadi, guncelleme durduruldu")

    if dest is None:
        dest = os.path.join(tempfile.gettempdir(),
                            "VideoDownloaderSetup-%s.exe" % int(time.time()))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + ".part"
    digest = hashlib.sha256()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            with open(part, "wb") as f:
                while True:
                    chunk = r.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if progress and total:
                        progress(done * 100.0 / total)

        got = digest.hexdigest()
        if got != expected:
            raise RuntimeError(
                "saglama toplami uyusmuyor (beklenen %s..., gelen %s...)"
                % (expected[:12], got[:12]))
        if os.path.getsize(part) < 1024 * 1024:
            raise RuntimeError("indirilen kurulum dosyasi cok kucuk")
    except Exception:
        # Dogrulanmamis dosyayi diskte birakma
        try:
            os.remove(part)
        except OSError:
            pass
        raise

    if os.path.exists(dest):
        os.remove(dest)
    os.rename(part, dest)
    return dest


# ---- Yama: zip'ten Range ile dosya cekme ----------------------------------

def app_dir():
    """Kurulu uygulamanin klasoru (sadece frozen modda)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return None


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_local_manifest(base):
    try:
        with open(os.path.join(base, MANIFEST_NAME), "r", encoding="utf-8") as f:
            return json.load(f).get("files") or {}
    except Exception:
        return {}


def _get_range(url, start, end, timeout=60):
    """[start, end] (iki uc dahil) baytlarini getirir.

    GitHub'in indirme sunucusu "bytes=-N" (son N bayt) bicimini desteklemiyor
    (501 donuyor), bu yuzden her zaman acik aralik istenir. Sunucu Range'i
    yok sayip tum dosyayi gonderirse (200) hata verilir; yama yolu terk
    edilir, tam kuruluma dusulur.
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Range": "bytes=%d-%d" % (start, end)})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if r.status != 206:
            raise RuntimeError("sunucu Range desteklemiyor (HTTP %s)" % r.status)
        data = r.read()
    if len(data) != end - start + 1:
        raise RuntimeError("eksik aralik: %d/%d bayt" % (len(data), end - start + 1))
    return data


def _zip_directory(url, size):
    """Zip'in merkezi dizinini okur: {ad: (yerel_baslik_ofseti, sikisik_boyut, yontem)}."""
    tail_len = min(size, 66 * 1024)      # EOCD + azami yorum
    tail = _get_range(url, size - tail_len, size - 1)
    pos = tail.rfind(b"PK\x05\x06")
    if pos < 0:
        raise RuntimeError("zip sonu bulunamadi")
    cd_size, cd_offset = struct.unpack("<II", tail[pos + 12:pos + 20])
    if cd_offset == 0xFFFFFFFF:
        raise RuntimeError("zip64 desteklenmiyor")
    cd = _get_range(url, cd_offset, cd_offset + cd_size - 1)

    entries = {}
    i = 0
    while i + 46 <= len(cd) and cd[i:i + 4] == b"PK\x01\x02":
        (method, csize, nlen, xlen, clen, lho) = (
            struct.unpack("<H", cd[i + 10:i + 12])[0],
            struct.unpack("<I", cd[i + 20:i + 24])[0],
            struct.unpack("<H", cd[i + 28:i + 30])[0],
            struct.unpack("<H", cd[i + 30:i + 32])[0],
            struct.unpack("<H", cd[i + 32:i + 34])[0],
            struct.unpack("<I", cd[i + 42:i + 46])[0],
        )
        name = cd[i + 46:i + 46 + nlen].decode("utf-8")
        entries[name] = (lho, csize, method)
        i += 46 + nlen + xlen + clen
    if not entries:
        raise RuntimeError("zip dizini bos")
    return entries, cd_offset


def _extract_entry(blob, base, name, csize, method):
    """blob icinde 'base' ofsetinde duran yerel kaydi acip dosya verisini dondurur."""
    hdr = blob[base:base + 30]
    if hdr[:4] != b"PK\x03\x04":
        raise RuntimeError("bozuk zip kaydi: %s" % name)
    nlen, xlen = struct.unpack("<HH", hdr[26:30])
    start = base + 30 + nlen + xlen
    raw = blob[start:start + csize]
    if len(raw) != csize:
        raise RuntimeError("zip kaydi eksik: %s" % name)
    if method == 0:
        return raw
    if method == 8:
        return zlib.decompress(raw, -15)
    raise RuntimeError("desteklenmeyen sikistirma (%d): %s" % (method, name))


def plan_patch(remote_files, base=None):
    """Hangi dosyalar inmeli, hangileri silinmeli?

    Yerel dosya, boyutu tutuyor VE (yerel manifestteki hash'i ya da manifest
    yoksa dosyanin kendi hash'i) uzak hash'le ayniysa atlanir. Silinecekler
    sadece eski manifestte kayitli olup yenisinde olmayan dosyalardir;
    kullanicinin klasore koydugu baska bir seye (downloads/ vb.) dokunulmaz.
    """
    base = base or app_dir()
    local = _read_local_manifest(base)
    need = []
    for rel, meta in remote_files.items():
        path = os.path.join(base, *rel.split("/"))
        try:
            if os.path.getsize(path) == meta["size"]:
                known = (local.get(rel) or {}).get("sha256")
                if known == meta["sha256"]:
                    continue
                if not known and _sha256_file(path) == meta["sha256"]:
                    continue
        except OSError:
            pass
        need.append(rel)
    delete = sorted(rel for rel in local if rel not in remote_files)
    return need, delete


def download_patch(info, dest_dir, progress=None):
    """Degisen dosyalari dest_dir/files altina indirip dogrular.

    Dondurur: (indirilen_dosya_sayisi, indirilen_bayt, silinecekler).
    """
    base = app_dir()
    if not base:
        raise RuntimeError("yama sadece kurulu uygulamada calisir")
    manifest = _get_json(info["manifest_url"], timeout=30)
    remote = manifest.get("files") or {}
    if manifest.get("version") != info["version"] or not remote:
        raise RuntimeError("manifest surumu uyusmuyor")

    need, delete = plan_patch(remote, base)
    shutil.rmtree(dest_dir, ignore_errors=True)
    files_dir = os.path.join(dest_dir, "files")
    os.makedirs(files_dir, exist_ok=True)

    downloaded = 0
    if need:
        entries, cd_offset = _zip_directory(info["bundle_url"], info["bundle_size"])
        missing = [r for r in need if r not in entries]
        if missing:
            raise RuntimeError("zip'te olmayan dosya: %s" % missing[0])

        # Her kaydin bittigi yer = zip'te kendinden sonra gelen kaydin
        # basladigi yer (son kayit icin merkezi dizinin basi). Yerel baslikta
        # ekstra alan merkezi dizindekinden farkli olabildigi icin boyutu
        # buradan hesaplamak en saglami.
        starts = sorted(lho for lho, _, _ in entries.values())
        nxt = {s: (starts[k + 1] if k + 1 < len(starts) else cd_offset)
               for k, s in enumerate(starts)}
        spans = sorted((entries[r][0], nxt[entries[r][0]], r) for r in need)

        groups = []
        for s, e, r in spans:
            if groups and s - groups[-1][1] <= RANGE_MERGE_GAP:
                groups[-1][1] = max(groups[-1][1], e)
                groups[-1][2].append(r)
            else:
                groups.append([s, e, [r]])

        total = sum(e - s for s, e, _ in groups) or 1
        for s, e, names in groups:
            blob = _get_range(info["bundle_url"], s, e - 1, timeout=120)
            for r in names:
                lho, csize, method = entries[r]
                data = _extract_entry(blob, lho - s, r, csize, method)
                meta = remote[r]
                if len(data) != meta["size"] or \
                        hashlib.sha256(data).hexdigest() != meta["sha256"]:
                    raise RuntimeError("hash uyusmuyor: %s" % r)
                out = os.path.join(files_dir, *r.split("/"))
                os.makedirs(os.path.dirname(out), exist_ok=True)
                with open(out, "wb") as f:
                    f.write(data)
            downloaded += e - s
            if progress:
                progress(downloaded * 100.0 / total)

    # Kurulumdaki manifest de yenilensin (bir sonraki yamanin temeli)
    with open(os.path.join(files_dir, MANIFEST_NAME), "w", encoding="utf-8") as f:
        json.dump(manifest, f)
    with open(os.path.join(dest_dir, "delete.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(r.replace("/", "\\") for r in delete))
    return len(need), downloaded, delete


# --------------------------------------------------------------------------
# Otomatik guncelleme
#
# Akis: uygulama acikken yeni surum arka planda hazirlanir ("staging"):
# yama ise degisen dosyalar, degilse kurulum dosyasi indirilir. Bir sonraki
# acilista launcher bunu gorur, uygulamayi kapatip dosyalari yerine koyar ve
# uygulamayi yeniden acar. Kullanici hicbir sey onaylamaz.
# --------------------------------------------------------------------------

def updates_dir():
    """Indirilen guncellemelerin durdugu klasor."""
    return os.path.join(user_data_dir(), "updates")


def get_auto_update():
    """Otomatik guncelleme acik mi? (varsayilan: acik)"""
    return bool(_read_state().get("auto_update", True))


def set_auto_update(enabled):
    state = _read_state()
    state["auto_update"] = bool(enabled)
    _write_state(state)
    return bool(enabled)


def _remove_path(p):
    if not p:
        return
    if os.path.isdir(p):
        shutil.rmtree(p, ignore_errors=True)
    else:
        try:
            os.remove(p)
        except OSError:
            pass


def clear_staged_update(remove_file=True):
    """Hazirlanmis guncellemeyi unut (ve dosyalarini sil)."""
    state = _read_state()
    staged = state.pop("staged_update", None)
    state.pop("staged_attempts", None)
    _write_state(state)
    if remove_file and staged:
        _remove_path(staged.get("path"))
    return staged


def staged_update():
    """Uygulanmaya hazir bekleyen guncelleme varsa bilgisini dondurur.

    Surum artik eski kaldiysa (guncelleme tuttu) ya da dosyalar kayboldusa
    kayit temizlenir.
    """
    staged = _read_state().get("staged_update")
    if not isinstance(staged, dict):
        return None
    path, version = staged.get("path"), staged.get("version")
    if not path or not version:
        clear_staged_update()
        return None
    if _version_tuple(version) <= _version_tuple(APP_VERSION):
        clear_staged_update()
        return None
    if staged.get("kind") == "patch":
        ok = os.path.isfile(os.path.join(path, "files", MANIFEST_NAME))
    else:
        try:
            ok = os.path.getsize(path) >= 1024 * 1024
        except OSError:
            ok = False
    if not ok:
        clear_staged_update()
        return None
    return staged


def _prune_updates_dir(keep=None):
    """updates/ klasorunde sadece guncel hazirlik kalsin."""
    d = updates_dir()
    if not os.path.isdir(d):
        return
    for name in os.listdir(d):
        p = os.path.join(d, name)
        if keep and os.path.abspath(p) == os.path.abspath(keep):
            continue
        _remove_path(p)


def _patch_possible(info):
    if not (info.get("manifest_url") and info.get("bundle_url")
            and info.get("bundle_size") and app_dir() and os.name == "nt"):
        return False
    # Bu surumun yamasi daha once iki kez uygulanamadiysa tam kuruluma gec
    return _read_state().get("patch_failed_version") != info["version"]


def stage_app_update(info=None, progress=None, force=False):
    """Yeni surumu arka planda indirip bir sonraki acilisa hazirla.

    Dondurur: hazirlanan guncelleme sozlugu ya da None.
    """
    state = _read_state()
    now = time.time()
    if info is None:
        if not force and now - state.get("app_checked_at", 0) < APP_CHECK_INTERVAL:
            return staged_update()
        info = app_update_available()
        state = _read_state()
        state["app_checked_at"] = now
        _write_state(state)
    if not info:
        return None

    existing = staged_update()
    if existing and existing.get("version") == info["version"]:
        return existing

    record = None
    if _patch_possible(info):
        dest = os.path.join(updates_dir(), "patch-%s" % info["version"])
        try:
            count, nbytes, delete = download_patch(info, dest, progress=progress)
            record = {"kind": "patch", "path": dest, "files": count,
                      "bytes": nbytes, "deleted": len(delete)}
        except Exception as e:
            _remove_path(dest)
            state = _read_state()
            state["last_patch_error"] = "%s: %s" % (info["version"], e)
            fails = state.get("patch_dl_failures") or {}
            fails = {info["version"]: int(fails.get(info["version"], 0)) + 1}
            state["patch_dl_failures"] = fails
            if fails[info["version"]] >= MAX_INSTALL_ATTEMPTS:
                # Ust uste inemedi (Range'i engelleyen proxy vb.): bu surum
                # icin tam kuruluma gec ki kullanici guncellemesiz kalmasin.
                state["patch_failed_version"] = info["version"]
            _write_state(state)
            if fails[info["version"]] < MAX_INSTALL_ATTEMPTS:
                # Gecici ag hatasi olabilir: 80 MB'a hemen dusme, bir
                # sonraki kontrolde yamayi tekrar dene.
                raise
            record = None
    if record is None:
        if not info.get("url"):
            return None
        dest = os.path.join(updates_dir(),
                            "VideoDownloaderSetup-%s.exe" % info["version"])
        path = download_app_installer(info["url"], info.get("sha256_url"),
                                      progress=progress, dest=dest)
        record = {"kind": "installer", "path": path}
    _prune_updates_dir(keep=record["path"])

    state = _read_state()
    record.update({
        "version": info["version"],
        "notes": (info.get("notes") or "")[:2000],
        "staged_at": now,
    })
    state["staged_update"] = record
    state["staged_attempts"] = 0
    _write_state(state)
    return record


# ---- Yamayi uygulama ------------------------------------------------------
#
# Eski yontem: uygulama kapanir, ayri bir cmd betigi dosyalari robocopy ile
# koyup uygulamayi yeniden acar. Kullanicinin bilgisayarinda betik hic
# calismadi (launcher.log: uygulama kapandi, dosyalar degismedi, geri
# acilmadi -- Ekim'deki sessiz kurulum denemeleri de ayni yerde dustu).
#
# Simdiki yontem dis betige guvenmiyor: Windows calisan bir exe'nin ya da
# yuklu bir DLL'in USTUNE YAZDIRMAZ ama ADINI DEGISTIRTIR. Uygulama her
# degisen dosyanin eskisini "<ad>.vdold" yapar, yenisini yerine kopyalar,
# kendini yeniden baslatir. (PyInstaller exe'deki PYZ arsivini her okumada
# acip kapatiyor; exe acik tutulmuyor.) Herhangi bir adim tutmazsa yapilan
# her sey geri alinir, eski surum saglam kalir. .vdold dosyalari bir sonraki
# acilista silinir (o an hala yuklu olduklari icin simdi silinemezler).

OLD_SUFFIX = ".vdold"


def _log_update(*parts):
    """launcher.log'a yaz (launcher'in _log'u ile ayni dosya)."""
    try:
        d = user_data_dir()
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "launcher.log"), "a", encoding="utf-8") as f:
            f.write("%s  [yama] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                        " ".join(str(p) for p in parts)))
    except Exception:
        pass


def _move_aside(path):
    """path'i path.vdold yap; eski bir .vdold kalmissa once onu temizle."""
    aside = path + OLD_SUFFIX
    if os.path.exists(aside):
        try:
            os.remove(aside)
        except OSError:
            # Onceki guncellemeden kalan ve hala yuklu olan bir kopya olabilir
            n = 2
            while os.path.exists("%s%s%d" % (path, OLD_SUFFIX, n)):
                n += 1
            aside = "%s%s%d" % (path, OLD_SUFFIX, n)
    os.rename(path, aside)
    return aside


def apply_patch_in_place(staged, base=None):
    """Hazirlanan yamayi kurulum klasorune uygular. Basarisizsa geri alir.

    Dondurur: True (uygulandi) ya da False (hicbir sey degismedi).
    """
    base = base or app_dir()
    if not base:
        return False
    src_root = os.path.join(staged["path"], "files")
    rels = []
    for root, _dirs, names in os.walk(src_root):
        for name in names:
            rels.append(os.path.relpath(os.path.join(root, name), src_root))
    try:
        with open(os.path.join(staged["path"], "delete.txt"), "r", encoding="utf-8") as f:
            deletes = [ln.strip() for ln in f.read().splitlines() if ln.strip()]
    except OSError:
        deletes = []
    # Manifest en son yazilsin: yarida kalan bir guncelleme "guncel" sanilmasin.
    # exe ondan hemen once: surum numarasi exe'nin icinde.
    def order(rel):
        low = rel.replace("\\", "/").lower()
        return (low == MANIFEST_NAME, low.endswith(".exe") and "/" not in low, low)
    rels.sort(key=order)

    undo = []   # (yeni_dosya_ya_da_None, kenara_alinan_ya_da_None)
    try:
        for rel in rels:
            dst = os.path.join(base, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            aside = _move_aside(dst) if os.path.exists(dst) else None
            undo.append((dst, aside))
            shutil.copy2(os.path.join(src_root, rel), dst)
        for rel in deletes:
            dst = os.path.join(base, rel.replace("\\", os.sep).replace("/", os.sep))
            if os.path.isfile(dst):
                undo.append((None, _move_aside(dst)))
    except Exception as e:
        _log_update("uygulanamadi, geri aliniyor:", repr(e))
        for dst, aside in reversed(undo):
            try:
                if dst and os.path.exists(dst):
                    os.remove(dst)
            except OSError:
                pass
            try:
                if aside:
                    target = dst or aside[:aside.rindex(OLD_SUFFIX)]
                    os.rename(aside, target)
            except OSError as e2:
                _log_update("geri alinamadi:", aside, repr(e2))
        state = _read_state()
        state["last_patch_error"] = "%s uygulama: %s" % (staged.get("version"), e)
        _write_state(state)
        return False

    _log_update("uygulandi:", staged.get("version"), len(rels), "dosya,",
                len(deletes), "silindi")
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
            r"\{9C4E6A2F-3B7D-4E8A-9F1C-2B8D51E04A67}_is1",
            0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, staged["version"])
        winreg.CloseKey(key)
    except Exception:
        pass
    return True


def cleanup_old_files(base=None):
    """Onceki yamadan kalan .vdold dosyalarini sil (artik yuklu degiller)."""
    base = base or app_dir()
    if not base or not os.path.isdir(base):
        return 0
    removed = 0
    for root, _dirs, names in os.walk(base):
        for name in names:
            if OLD_SUFFIX in name:
                try:
                    os.remove(os.path.join(root, name))
                    removed += 1
                except OSError:
                    pass
    return removed


def _child_env():
    env = dict(os.environ)
    # PyInstaller: donmus bir uygulama kendi exe'sini baslatirken alt surecin
    # ust surecin ortam degiskenlerini (_PYI_*) devralmamasi icin.
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def _spawn_detached(args, env=None, shell_string=None):
    flags = 0
    for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
        flags |= getattr(subprocess, name, 0)
    subprocess.Popen(shell_string if shell_string is not None else args,
                     creationflags=flags, close_fds=True, env=env or _child_env())


def relaunch(exe):
    """Yeni exe'yi baslat; o, bu surec kapanana kadar bekler (--wait-pid)."""
    _spawn_detached([exe, "--wait-pid", str(os.getpid())])


def apply_staged_update(staged=None, relaunch_exe=None):
    """Bekleyen guncellemeyi uygular. True donerse uygulama HEMEN kapanmalidir.

    Yama: dosyalar bu surecte yerine konur, yeni surum baslatilir.
    Kurulum: sessiz kurulum ayri surecte calisir, bitince uygulamayi acar.
    """
    staged = staged or staged_update()
    if not staged or os.name != "nt":
        return False

    state = _read_state()
    attempts = int(state.get("staged_attempts", 0))
    if attempts >= MAX_INSTALL_ATTEMPTS:
        if staged.get("kind") == "patch":
            # Yama iki kez tutmadi: bu surum icin tam kuruluma gec. Bir
            # sonraki kontrol Kurulum.exe'yi hazirlar.
            state["patch_failed_version"] = staged["version"]
            state["app_checked_at"] = 0
            _write_state(state)
            clear_staged_update()
            _log_update("iki deneme basarisiz, tam kuruluma geciliyor:", staged["version"])
        # Kurulum da iki kez tutmadiysa acilis dongusune girmemek icin
        # vazgec; kullanici Ayarlar'dan elle kurabilir.
        return False
    state["staged_attempts"] = attempts + 1
    _write_state(state)

    exe = relaunch_exe
    if exe is None and getattr(sys, "frozen", False):
        exe = sys.executable
    if not exe:
        return False

    if staged.get("kind") == "patch":
        if not apply_patch_in_place(staged):
            return False
        try:
            relaunch(exe)
        except Exception as e:
            # Dosyalar yeni surumde; uygulama bu oturumda eski kodla devam
            # eder, bir sonraki acilista yeni surum acilir.
            _log_update("yeniden baslatilamadi:", repr(e))
            return False
        return True

    # Tam kurulum. Komut tek bir dize olarak verilir: liste verilirse Python
    # icteki tirnaklari \" diye kacirir, cmd bunu anlamaz ve komut bozulur
    # (Ekim'deki "sessiz kurulum baslatilamadi" dongusu buydu).
    # "&": kurulum basarisiz olsa bile uygulama geri gelsin.
    inner = '"%s" %s & start "" "%s"' % (staged["path"], " ".join(SILENT_FLAGS), exe)
    try:
        _spawn_detached(None, shell_string='cmd /c "%s"' % inner)
    except Exception as e:
        _log_update("kurulum baslatilamadi:", repr(e))
        return False
    return True


# --------------------------------------------------------------------------
# yt-dlp-ejs (YouTube JS cozucu betikleri)
#
# yt-dlp, YouTube'un sifre bulmacalarini harici bir JS calistiricisinda
# (bizde paketlenen quickjs) cozer. Cozucu betikler yt-dlp-ejs paketinden
# gelir ve yt-dlp bunlarin SURUMUNU ve HASH'ini dogrular
# (yt_dlp/extractor/youtube/jsc/_builtin/vendor/_info.py). Yani yt-dlp
# kendini guncelleyip yeni bir ejs surumu isterse, eski ejs reddedilir ve
# girisli YouTube indirmeleri "Requested format is not available" ile
# bozulur. Bu yuzden ejs, yt-dlp'nin istedigi TAM surume getirilir.
# --------------------------------------------------------------------------

def _runtime_dirs():
    """activate() ile ayni oncelik: once kullanici, sonra paket."""
    return [d for d in (user_runtime_dir(), bundled_runtime_dir()) if d]


def _read_version(path, pattern):
    try:
        with open(path, "r", encoding="utf-8") as f:
            m = re.search(pattern, f.read(), re.M)
        return m.group(1) if m else None
    except Exception:
        return None


def required_ejs_version():
    """Etkin yt-dlp kopyasinin bekledigi yt-dlp-ejs surumu."""
    for d in _runtime_dirs():
        pkg = os.path.join(d, "yt_dlp")
        if _looks_like_ytdlp(pkg):
            return _read_version(
                os.path.join(pkg, "extractor", "youtube", "jsc", "_builtin",
                             "vendor", "_info.py"),
                r"^VERSION\s*=\s*['\"]([^'\"]+)")
    return None


def installed_ejs_version():
    """import yt_dlp_ejs'in yukleyecegi kopyanin surumu."""
    for d in _runtime_dirs():
        v = _read_version(os.path.join(d, "yt_dlp_ejs", "_version.py"),
                          r"version\s*=\s*['\"]([^'\"]+)")
        if v:
            return v
    return None


def ensure_ejs():
    """yt-dlp-ejs'i yt-dlp'nin istedigi surume getir.

    Agi sadece surumler uyusmadiginda kullanir. Dondurur: (degisti_mi, mesaj).
    """
    required = required_ejs_version()
    if not required:
        # Gelistirme modu (runtime klasoru yok): pip'teki paketler kullanilir
        return False, "ejs: runtime yok, atlandi"
    current = installed_ejs_version()
    if current == required:
        return False, "ejs guncel (%s)" % current

    data = _get_json(PYPI_EJS_JSON % required)
    wheel_url = None
    for u in data.get("urls", []):
        if u.get("packagetype") == "bdist_wheel":
            wheel_url = u["url"]
            break
    if not wheel_url:
        raise RuntimeError("yt-dlp-ejs %s wheel bulunamadi" % required)

    runtime = user_runtime_dir()
    os.makedirs(runtime, exist_ok=True)
    staging = os.path.join(runtime, ".staging-ejs")
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging, exist_ok=True)
    try:
        whl = os.path.join(staging, "ejs.whl")
        with open(whl, "wb") as f:
            f.write(_get(wheel_url, timeout=60))
        with zipfile.ZipFile(whl) as z:
            z.extractall(staging)
        new_pkg = os.path.join(staging, "yt_dlp_ejs")
        if not os.path.isfile(os.path.join(new_pkg, "yt", "solver", "lib.min.js")):
            raise RuntimeError("indirilen yt-dlp-ejs eksik gorunuyor")

        target = os.path.join(runtime, "yt_dlp_ejs")
        old = target + ".old"
        shutil.rmtree(old, ignore_errors=True)
        if os.path.isdir(target):
            os.rename(target, old)
        shutil.move(new_pkg, target)
        shutil.rmtree(old, ignore_errors=True)
        return True, "yt-dlp-ejs %s -> %s" % (current or "yok", required)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
