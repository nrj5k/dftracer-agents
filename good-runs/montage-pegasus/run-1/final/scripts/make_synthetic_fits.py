import struct, sys

def card(key, val, comment=""):
    if isinstance(val, bool):
        vs = "T" if val else "F"
        s = f"{key:<8}= {vs:>20} / {comment}"
    elif isinstance(val, int):
        s = f"{key:<8}= {val:>20d} / {comment}"
    elif isinstance(val, float):
        s = f"{key:<8}= {val:>20.10f} / {comment}"
    elif key == "COMMENT" or key == "":
        s = f"{key:<8}{val}"
    else:
        vs = f"'{val}'"
        s = f"{key:<8}= {vs:<20} / {comment}"
    return s[:80].ljust(80)

def make_fits(path, nx, ny, crval1, crval2, cdelt=0.001, value=100.0):
    cards = []
    cards.append(card("SIMPLE", True, "conforms to FITS standard"))
    cards.append(card("BITPIX", -32, "32-bit float"))
    cards.append(card("NAXIS", 2, "2D image"))
    cards.append(card("NAXIS1", nx, ""))
    cards.append(card("NAXIS2", ny, ""))
    cards.append(card("CTYPE1", "RA---TAN", ""))
    cards.append(card("CTYPE2", "DEC--TAN", ""))
    cards.append(card("CRPIX1", float(nx)/2.0, ""))
    cards.append(card("CRPIX2", float(ny)/2.0, ""))
    cards.append(card("CRVAL1", crval1, ""))
    cards.append(card("CRVAL2", crval2, ""))
    cards.append(card("CDELT1", -cdelt, ""))
    cards.append(card("CDELT2", cdelt, ""))
    cards.append(card("CROTA2", 0.0, ""))
    cards.append(card("EQUINOX", 2000.0, ""))
    cards.append("END".ljust(80))
    header = "".join(cards)
    # pad header to multiple of 2880
    while len(header) % 2880 != 0:
        header += " " * 80
    data = bytearray()
    import math
    for j in range(ny):
        for i in range(nx):
            v = value + i + j
            data += struct.pack(">f", v)
    while len(data) % 2880 != 0:
        data += b"\x00"
    with open(path, "wb") as f:
        f.write(header.encode("ascii"))
        f.write(data)

if __name__ == "__main__":
    out, nx, ny, crval1, crval2 = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4]), float(sys.argv[5])
    make_fits(out, nx, ny, crval1, crval2)
    print("wrote", out)
