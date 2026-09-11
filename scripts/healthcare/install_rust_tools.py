#!/usr/bin/env python3
"""Install checksum-pinned upstream tools on the public Linux x86_64 runner."""

import hashlib
import io
from pathlib import Path
import platform
import sys
import tarfile
import urllib.request


TOOLS = (
    (
        "https://github.com/taiki-e/cargo-llvm-cov/releases/download/v0.8.7/"
        "cargo-llvm-cov-x86_64-unknown-linux-gnu.tar.gz",
        "9a75fe29538d3800b3da57f6f6efb64cba5c720a257bf0cb8b51f39d495a9168",
        "cargo-llvm-cov",
    ),
    (
        "https://github.com/rustsec/rustsec/releases/download/cargo-audit/v0.22.2/"
        "cargo-audit-x86_64-unknown-linux-gnu-v0.22.2.tgz",
        "ab28a1bdb54db4d5d8ad5981cf1f959410370b3d28250dbd35f6a44248620e39",
        "cargo-audit-x86_64-unknown-linux-gnu-v0.22.2/cargo-audit",
    ),
)


def install_archive(data, checksum, member_name, destination):
    if hashlib.sha256(data).hexdigest() != checksum:
        raise ValueError("Rust tool archive checksum mismatch")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        member = archive.getmember(member_name)
        if not member.isfile():
            raise ValueError("Rust tool archive member must be a regular file")
        # Copy only the reviewed member; archive paths and links are never extracted.
        binary = destination / Path(member_name).name
        with binary.open("xb") as output:
            output.write(archive.extractfile(member).read())
        binary.chmod(0o755)


def main(destination):
    if (platform.system(), platform.machine()) != ("Linux", "x86_64"):
        raise ValueError("Rust validation tools require native Linux x86_64")
    destination.mkdir(parents=True, exist_ok=True)
    for url, checksum, member in TOOLS:
        with urllib.request.urlopen(url, timeout=60) as response:
            install_archive(response.read(), checksum, member, destination)
        print(f"Installed {Path(member).name} from verified archive sha256:{checksum}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
