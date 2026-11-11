"""Ed25519：RFC 8032 标准测试向量与篡改检查。离线。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import ed25519

VECTORS = [  # RFC 8032 7.1 TEST 1、TEST 2
    ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
     "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
]


class Ed25519Tests(unittest.TestCase):
    def test_rfc8032_vectors(self):
        for sk, pk, msg, sig in VECTORS:
            sk, pk, msg, sig = (bytes.fromhex(x) for x in (sk, pk, msg, sig))
            self.assertEqual(ed25519.public_key(sk), pk)
            self.assertEqual(ed25519.sign(sk, msg), sig)
            self.assertTrue(ed25519.verify(pk, msg, sig))

    def test_tampering_is_rejected(self):
        sk, pk, _, _ = (bytes.fromhex(x) for x in VECTORS[1])
        msg = "大肥鱼 1.5.1".encode("utf-8")
        sig = ed25519.sign(sk, msg)
        self.assertTrue(ed25519.verify(pk, msg, sig))
        self.assertFalse(ed25519.verify(pk, msg + b" ", sig))
        bad = bytearray(sig)
        bad[40] ^= 1
        self.assertFalse(ed25519.verify(pk, msg, bytes(bad)))
        other = ed25519.public_key(bytes(32))
        self.assertFalse(ed25519.verify(other, msg, sig))

    def test_malformed_inputs_return_false(self):
        pk = bytes.fromhex(VECTORS[0][1])
        self.assertFalse(ed25519.verify(pk, b"", b"\x00" * 10))
        self.assertFalse(ed25519.verify(b"\x01" * 5, b"", b"\x00" * 64))
        self.assertFalse(ed25519.verify(b"\xff" * 32, b"", b"\x00" * 64))


if __name__ == "__main__":
    unittest.main()
