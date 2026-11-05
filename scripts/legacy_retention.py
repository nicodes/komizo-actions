#!/usr/bin/env python3
"""Check the existing AGE pair before retaining ciphertext; never decrypt it."""
import argparse
import hashlib
import os
from pathlib import Path
import re
import stat


def verify(directory):
    if directory.is_symlink() or any(path.is_symlink() for path in directory.parents):
        raise ValueError('legacy retention must not traverse links')
    parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if set(os.listdir(parent)) != {'pb_data.tar.age', 'metadata'}:
            raise ValueError('legacy retention requires exactly the encrypted AGE pair')
        def read(name, maximum):
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
                os.close(descriptor)
                raise ValueError('legacy retention requires bounded regular ciphertext and metadata')
            return os.fdopen(descriptor, 'rb')
        with read('metadata', 65536) as stream:
            metadata = stream.read().decode('utf-8')
        expected = re.findall(r'^encrypted_sha256=([a-f0-9]{64})$', metadata, re.M)
        if len(expected) != 1:
            raise ValueError('legacy metadata requires one encrypted checksum')
        with read('pb_data.tar.age', 17 * 1024 ** 3) as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if expected[0] != actual:
            raise ValueError('legacy encrypted checksum mismatch')
        return actual
    finally:
        os.close(parent)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    try:
        print('retained_encrypted_sha256=' + verify(args.directory))
    except (OSError, UnicodeError, ValueError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
