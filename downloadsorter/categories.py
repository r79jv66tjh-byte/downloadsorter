"""Kategorien und Dateityp-Erkennung (Endung + Magic Bytes)."""
from __future__ import annotations

import os
import zipfile

INSTALLER = "Installer"
ARCHIVE = "Archive"
DOCUMENT = "Dokumente"
IMAGE = "Bilder"
VIDEO = "Videos"
MUSIC = "Musik"
TORRENT = "Torrents"
CODE = "Code"
OTHER = "Sonstiges"
INCOMPLETE = "Unvollständig"  # abgebrochene Downloads; wird intern als Unterkategorie von Sonstiges geführt

CATEGORIES = [INSTALLER, ARCHIVE, DOCUMENT, IMAGE, VIDEO, MUSIC, TORRENT, CODE, OTHER]

_EXT = {
    INSTALLER: "exe msi msix msixbundle appx appxbundle apk xapk dmg pkg deb rpm appimage msu cab",
    ARCHIVE: "zip rar 7z tar gz tgz bz2 tbz2 xz txz zst lz lzma iso img vhd vhdx z arj",
    DOCUMENT: "pdf doc docx odt rtf txt md xls xlsx xlsm ods csv ppt pptx odp epub mobi azw3 djvu "
              "pages numbers key xps oxps one msg eml ics vcf log",
    IMAGE: "jpg jpeg png gif bmp webp tif tiff heic heif avif svg ico psd raw cr2 cr3 nef arw dng jfif xcf",
    VIDEO: "mp4 mkv avi mov wmv flv webm m4v mpg mpeg 3gp ts m2ts vob ogv",
    MUSIC: "mp3 flac wav ogg oga opus m4a aac wma aiff aif mid midi alac",
    TORRENT: "torrent magnet",
    CODE: "py pyw js mjs ts tsx jsx java jar class c h cpp hpp cc cs go rs rb php html htm css scss "
          "json xml yaml yml toml ini cfg sql sh bat cmd ps1 psm1 lua kt swift ipynb gradle sln csproj "
          "vcxproj dll so dylib wasm reg vbs",
}
EXT_TO_CATEGORY = {e: cat for cat, exts in _EXT.items() for e in exts.split()}

# Endungen abgebrochener/laufender Downloads
INCOMPLETE_EXT = {"crdownload", "part", "partial", "tmp", "download", "opdownload", "!ut", "bc!", "aria2"}

# Zip-basierte Formate, bei denen die Endung Vorrang vor der Magic-Erkennung "zip" hat
_ZIP_FAMILY = {"docx", "xlsx", "xlsm", "pptx", "odt", "ods", "odp", "epub", "jar", "apk", "xapk",
               "msix", "msixbundle", "appx", "appxbundle", "ipynb", "pages", "numbers", "key", "xps", "oxps"}
_TEXT_EXT = {"txt", "log", "csv", "json", "xml", "html", "htm", "svg", "md", "ini", "cfg", "py", "js"}
# OLE-Container (D0 CF 11 E0): msi, doc, xls, ppt, msg ...
_OLE_FAMILY = {"msi", "doc", "xls", "ppt", "msg", "msu", "one"}


def extension(name: str) -> str:
    ext = os.path.splitext(name)[1]
    return ext[1:].lower() if ext else ""


def category_from_extension(name: str) -> str | None:
    return EXT_TO_CATEGORY.get(extension(name))


def _zip_kind(path: str) -> str:
    """Unterscheidet Office-/Epub-/Java-Dateien von normalen Zip-Archiven."""
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()[:400]
    except Exception:
        return ARCHIVE
    joined = "\n".join(names)
    if any(n.startswith(("word/", "xl/", "ppt/")) for n in names) or "mimetype" in names:
        return DOCUMENT  # OOXML / ODF / EPUB
    if "AndroidManifest.xml" in names:
        return INSTALLER
    if "META-INF/MANIFEST.MF" in names and ".class" in joined:
        return CODE
    return ARCHIVE


def sniff(path: str) -> tuple[str | None, str | None]:
    """Erkennt den Typ per Dateiinhalt. Liefert (Kategorie, Typbezeichnung) oder (None, None)."""
    try:
        with open(path, "rb") as f:
            head = f.read(64)
            iso = b""
            if len(head) == 64:
                f.seek(0x8001)
                iso = f.read(5)
    except OSError:
        return None, None
    if not head:
        return None, None

    def at(off: int, sig: bytes) -> bool:
        return head[off:off + len(sig)] == sig

    if at(0, b"MZ"):
        return INSTALLER, "Windows-Programm (EXE/DLL)"
    if at(0, b"PK\x03\x04") or at(0, b"PK\x05\x06"):
        kind = _zip_kind(path)
        return kind, {DOCUMENT: "Office/ODF/EPUB (Zip)", INSTALLER: "Android-Paket",
                      CODE: "Java-Archiv"}.get(kind, "ZIP-Archiv")
    if at(0, b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return DOCUMENT, "OLE-Container (Office alt / MSI)"
    if at(0, b"Rar!\x1a\x07"):
        return ARCHIVE, "RAR-Archiv"
    if at(0, b"7z\xbc\xaf\x27\x1c"):
        return ARCHIVE, "7-Zip-Archiv"
    if at(0, b"\x1f\x8b"):
        return ARCHIVE, "GZip"
    if at(0, b"BZh"):
        return ARCHIVE, "BZip2"
    if at(0, b"\xfd7zXZ\x00"):
        return ARCHIVE, "XZ"
    if at(0, b"\x28\xb5\x2f\xfd"):
        return ARCHIVE, "Zstandard"
    if at(0, b"MSCF"):
        return INSTALLER, "Cabinet (CAB)"
    if iso == b"CD001":
        return ARCHIVE, "ISO-Abbild"
    if at(0, b"%PDF"):
        return DOCUMENT, "PDF"
    if at(0, b"{\\rtf"):
        return DOCUMENT, "RTF"
    if at(0, b"\x89PNG\r\n\x1a\n"):
        return IMAGE, "PNG"
    if at(0, b"\xff\xd8\xff"):
        return IMAGE, "JPEG"
    if at(0, b"GIF87a") or at(0, b"GIF89a"):
        return IMAGE, "GIF"
    if at(0, b"BM") and len(head) > 14 and head[6:10] == b"\x00\x00\x00\x00":
        return IMAGE, "Bitmap"
    if at(0, b"II*\x00") or at(0, b"MM\x00*"):
        return IMAGE, "TIFF/RAW"
    if at(0, b"\x00\x00\x01\x00"):
        return IMAGE, "Icon"
    if at(0, b"8BPS"):
        return IMAGE, "Photoshop"
    if at(0, b"RIFF"):
        sub = head[8:12]
        if sub == b"WEBP":
            return IMAGE, "WebP"
        if sub == b"AVI ":
            return VIDEO, "AVI"
        if sub == b"WAVE":
            return MUSIC, "WAV"
    if at(4, b"ftyp"):
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"mif1", b"msf1", b"avif", b"heim", b"heis"):
            return IMAGE, "HEIF/AVIF"
        if brand in (b"M4A ", b"M4B ", b"M4P "):
            return MUSIC, "M4A"
        return VIDEO, "MP4/MOV"
    if at(0, b"\x1a\x45\xdf\xa3"):
        return VIDEO, "Matroska/WebM"
    if at(0, b"FLV"):
        return VIDEO, "FLV"
    if at(0, b"\x30\x26\xb2\x75\x8e\x66\xcf\x11"):
        return VIDEO, "ASF/WMV/WMA"
    if at(0, b"\x00\x00\x01\xba") or at(0, b"\x00\x00\x01\xb3"):
        return VIDEO, "MPEG"
    if at(0, b"ID3") or (head[0] == 0xFF and (head[1] & 0xE0) == 0xE0 and head[1] not in (0xFF,)):
        if not at(0, b"\xff\xd8"):
            return MUSIC, "MP3"
    if at(0, b"fLaC"):
        return MUSIC, "FLAC"
    if at(0, b"OggS"):
        return MUSIC, "Ogg"
    if at(0, b"MThd"):
        return MUSIC, "MIDI"
    if head.startswith(b"d8:announce") or head.startswith(b"d13:announce-list") or head.startswith(b"d4:info"):
        return TORRENT, "BitTorrent"
    if at(0, b"\x7fELF"):
        return CODE, "Linux-Programm (ELF)"
    if at(0, b"\x00asm"):
        return CODE, "WebAssembly"
    return None, None


def classify(path: str, name: str) -> tuple[str, str, bool]:
    """Bestimmt (Kategorie, Typbeschreibung, Endung_passt_nicht)."""
    ext = extension(name)
    if ext in INCOMPLETE_EXT:
        return OTHER, "Abgebrochener Download", False
    by_ext = EXT_TO_CATEGORY.get(ext)
    by_magic, desc = sniff(path)

    if by_magic is None:
        return (by_ext or OTHER), (f".{ext}" if ext else "unbekannt"), False
    if by_ext is None:
        # keine oder unbekannte Endung -> Inhalt entscheidet
        return by_magic, desc + (" (ohne Endung)" if not ext else f" (Endung .{ext})"), False
    if ext in _TEXT_EXT:
        # Textformate haben keine Signatur; eine echte Binär-Signatur heißt: falsche Endung
        return by_magic, desc, True
    if by_ext == by_magic:
        return by_ext, desc, False
    # Container-Formate: Endung ist genauer als die Signatur
    if desc and desc.startswith(("ZIP", "Office/ODF", "Java", "Android")) and ext in _ZIP_FAMILY:
        return by_ext, desc, False
    if desc and desc.startswith("OLE") and ext in _OLE_FAMILY:
        return by_ext, desc, False
    if by_magic == INSTALLER and desc.startswith("Windows-Programm") and by_ext == CODE and ext == "dll":
        return CODE, desc, False
    return by_magic, desc, True
