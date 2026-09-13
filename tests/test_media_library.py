from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from collections import namedtuple
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageCms, PngImagePlugin

import media_library
from media_library import MediaLibrary


def image_bytes(size=(12, 8), color=(255, 0, 0, 255), format="PNG", **options):
    image = Image.new("RGBA", size, color)
    if format in ("JPEG", "HEIF"):
        image = image.convert("RGB")
    output = io.BytesIO()
    image.save(output, format=format, **options)
    image.close()
    return output.getvalue()


class MediaLibraryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "library"
        self.library = MediaLibrary(self.root)

    def upload(self, name="Photo", **options):
        return self.library.upload(io.BytesIO(image_bytes(**options)), "photo.png", name)

    def test_uploads_normalized_images_and_persists_presets_and_names(self):
        first = self.upload("Zebra")
        second = self.upload("apple")
        third = self.library.upload(io.BytesIO(image_bytes(format="WEBP")), r"C:\phone\Team Night.webp")
        self.assertEqual(third["name"], "Team Night")
        updated = self.library.update(first["id"], " Mother's Day ", preset=True)
        self.assertTrue(updated["preset"])
        self.assertEqual(updated["name"], "Mother's Day")
        self.assertEqual([item["id"] for item in self.library.list()], [first["id"], second["id"], third["id"]])
        reopened = MediaLibrary(self.root)
        self.assertEqual(reopened.get(first["id"]), updated)
        self.assertEqual(updated["size_bytes"], reopened.path(first["id"]).stat().st_size)
        self.assertRegex(first["id"], r"^[0-9a-f]{32}$")
        self.assertNotIn("path", first)
        first["name"] = "Changed return value"
        self.assertEqual(reopened.get(first["id"])["name"], "Mother's Day")

    def test_exif_orientation_and_sensitive_metadata_are_not_retained(self):
        exif = Image.Exif()
        exif[274] = 6  # Portrait rotation applied before dimensions/preview are stored.
        exif[270] = "Private location comment"
        exif[34853] = {1: "N", 2: (1.0, 2.0, 3.0)}
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        encoded = image_bytes(size=(12, 8), format="JPEG", exif=exif, icc_profile=profile)
        item = self.library.upload(io.BytesIO(encoded), "phone.jpg")
        self.assertEqual((item["width"], item["height"]), (8, 12))
        for thumbnail in (False, True):
            with Image.open(self.library.path(item["id"], thumbnail)) as image:
                self.assertEqual(image.format, "PNG")
                self.assertEqual(dict(image.getexif()), {})
                self.assertNotIn("icc_profile", image.info)
                self.assertNotIn("exif", image.info)
            self.assertNotIn(b"Private location", self.library.path(item["id"], thumbnail).read_bytes())

    def test_png_text_and_transparency_metadata_are_reencoded_as_pixels(self):
        text = PngImagePlugin.PngInfo()
        text.add_text("Location", "Private venue")
        item = self.library.upload(io.BytesIO(image_bytes(pnginfo=text)), "photo.png")
        with Image.open(self.library.path(item["id"])) as image:
            self.assertNotIn("Location", image.info)
        self.assertNotIn(b"Private venue", self.library.path(item["id"]).read_bytes())

    def test_heic_is_decoded_and_stored_as_png(self):
        item = self.library.upload(io.BytesIO(image_bytes(format="HEIF")), "phone.heic")
        self.assertEqual((item["width"], item["height"]), (12, 8))
        with Image.open(self.library.path(item["id"])) as image:
            self.assertEqual(image.format, "PNG")

    def test_thumbnail_bounds_and_opaque_letterboxing_preserve_aspect_ratio(self):
        item = self.upload(size=(600, 600), color=(255, 0, 0, 128))
        with Image.open(self.library.path(item["id"], True)) as thumbnail:
            self.assertEqual(thumbnail.size, (270, 270))
        frame = self.library.frame(item["id"], 8, 4)
        self.assertEqual(len(frame), 8 * 4 * 4)
        with Image.frombytes("RGBA", (8, 4), frame) as image:
            self.assertEqual(image.getpixel((0, 2)), (0, 0, 0, 255))
            self.assertEqual(image.getpixel((7, 2)), (0, 0, 0, 255))
            self.assertEqual(image.getpixel((3, 2)), (128, 0, 0, 255))
            self.assertEqual(image.getextrema()[3], (255, 255))

    def test_rejects_empty_malformed_unsupported_truncated_and_animated_files(self):
        gif = io.BytesIO()
        Image.new("RGB", (2, 2)).save(gif, format="GIF")
        animated = io.BytesIO()
        Image.new("RGBA", (2, 2), "red").save(
            animated, format="PNG", save_all=True, append_images=[Image.new("RGBA", (2, 2), "blue")]
        )
        for content in (b"", b"not an image", gif.getvalue(), image_bytes()[:40], animated.getvalue()):
            with self.subTest(content=content[:8]):
                with self.assertRaises(ValueError):
                    self.library.upload(io.BytesIO(content), "looks-valid.png")
        self.assertEqual(self.library.list(), [])
        self.assertEqual(list((self.root / "images").iterdir()), [])

    def test_disguised_scripts_documents_and_executables_are_rejected(self):
        for content in (
            b'MZ\x90\x00executable placeholder', b'<html><script>alert(1)</script></html>',
            b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>',
            b'PK\x03\x04archive placeholder', b'%PDF-1.7 document placeholder',
        ):
            with self.subTest(content=content[:8]), self.assertRaises(ValueError):
                self.library.upload(io.BytesIO(content), 'photo.png')
        self.assertEqual(self.library.list(), [])
        self.assertEqual(list((self.root / 'images').iterdir()), [])
        self.assertEqual(list((self.root / 'thumbnails').iterdir()), [])

    def test_original_filename_and_appended_content_never_reach_stored_files(self):
        script = b'<script>window.uploadPayload = true</script>'
        item = self.library.upload(io.BytesIO(image_bytes() + script),
                                   '../../outside.html', '<img src=x onerror=alert(1)>')
        # Display names remain text; they are never interpreted as paths or code.
        self.assertEqual(item['name'], '<img src=x onerror=alert(1)>')
        for thumbnail in (False, True):
            path = self.library.path(item['id'], thumbnail)
            self.assertRegex(path.name, r'^[0-9a-f]{32}\.png$')
            self.assertTrue(path.is_relative_to(self.root))
            self.assertNotIn(script, path.read_bytes())
            with Image.open(path) as decoded:
                self.assertEqual(decoded.format, 'PNG')
                decoded.verify()
        self.assertFalse((self.root.parent / 'outside.html').exists())

    def test_dimensions_and_buffer_size_are_rechecked_after_decode(self):
        class ChangingImage:
            size, mode, n_frames = (2, 2), 'RGB', 1

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def getbands(self):
                return tuple(self.mode)

            def load(self):
                self.size, self.mode = self.after

        for after, message in (((8000, 8000), 'RGB'), 'megapixel'), (((4, 4), 'F'), 'memory limit'):
            source = ChangingImage()
            source.after = after
            with self.subTest(after=after), \
                    patch.object(media_library, 'MAX_DECODED_BYTES', 50), \
                    patch.object(media_library.Image, 'open', return_value=source), \
                    patch.object(media_library.ImageOps, 'exif_transpose') as orient:
                with self.assertRaisesRegex(ValueError, message):
                    self.library.upload(io.BytesIO(b'placeholder'), 'photo.heic')
                orient.assert_not_called()
        self.assertEqual(self.library.list(), [])

    def test_normalized_png_has_an_encoded_size_limit(self):
        with patch.object(media_library, 'MAX_STORED_IMAGE_BYTES', 40):
            with self.assertRaisesRegex(ValueError, 'Prepared image exceeds'):
                self.upload()
        self.assertEqual(self.library.list(), [])
        self.assertEqual(list((self.root / 'images').iterdir()), [])

    def test_storage_quota_counts_normalized_images_thumbnails_and_orphans(self):
        first = self.upload()
        pair_bytes = sum(self.library.path(first['id'], thumbnail).stat().st_size for thumbnail in (False, True))
        with patch.object(media_library, 'MAX_LIBRARY_BYTES', pair_bytes * 2 - 1):
            with self.assertRaisesRegex(ValueError, 'storage limit'):
                self.upload('Over quota')
        self.assertEqual(self.library.list(), [first])
        with patch.object(media_library, 'MAX_LIBRARY_BYTES', pair_bytes):
            # Existing assets can still be read/deleted; deletion restores capacity.
            self.assertEqual(self.library.get(first['id']), first)
            self.library.delete(first['id'])
            self.assertEqual(self.upload('Replacement')['name'], 'Replacement')
        self.library.delete(self.library.list()[0]['id'])
        (self.root / 'images' / '.orphan.tmp').write_bytes(b'x' * pair_bytes)
        with patch.object(media_library, 'MAX_LIBRARY_BYTES', pair_bytes + 1):
            with self.assertRaisesRegex(ValueError, 'storage limit'):
                self.upload()
        self.assertEqual(self.library.list(), [])

    def test_image_quota_is_checked_before_decode_and_serializes_final_slot(self):
        another = MediaLibrary(self.root)

        def attempt(library):
            try:
                return library.upload(io.BytesIO(image_bytes()), 'photo.png')
            except ValueError as exc:
                return str(exc)

        with patch.object(media_library, 'MAX_LIBRARY_IMAGES', 1):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(attempt, (self.library, another)))
            self.assertEqual(sum(isinstance(item, dict) for item in results), 1)
            self.assertEqual(sum(isinstance(item, str) and 'image limit' in item for item in results), 1)
            with patch.object(media_library, '_decode') as decode:
                with self.assertRaisesRegex(ValueError, 'image limit'):
                    self.upload()
                decode.assert_not_called()
        self.assertEqual(len(self.library.list()), 1)

    def test_low_disk_space_is_checked_before_decode_and_again_before_write(self):
        DiskUsage = namedtuple('DiskUsage', 'total used free')
        reserve = media_library.MIN_FREE_DISK_BYTES
        with patch.object(media_library.shutil, 'disk_usage', return_value=DiskUsage(0, 0, reserve)), \
                patch.object(media_library, '_decode') as decode:
            with self.assertRaisesRegex(ValueError, 'low on free storage'):
                self.upload()
            decode.assert_not_called()
        with patch.object(media_library.shutil, 'disk_usage', side_effect=[
            DiskUsage(0, 0, reserve + 100_000), DiskUsage(0, 0, reserve + 4096),
        ]):
            with self.assertRaisesRegex(ValueError, 'low on free storage'):
                self.upload()
        self.assertEqual(self.library.list(), [])
        self.assertEqual(list((self.root / 'images').iterdir()), [])
        self.assertEqual(list((self.root / 'thumbnails').iterdir()), [])

    def test_upload_reads_only_the_byte_limit_plus_one_and_checks_decoded_pixels(self):
        class Stream:
            def __init__(self):
                self.read_size = None

            def read(self, size):
                self.read_size = size
                return b"x" * size

        stream = Stream()
        with patch.object(media_library, "MAX_UPLOAD_BYTES", 100):
            with self.assertRaisesRegex(ValueError, "upload limit"):
                self.library.upload(stream, "large.png")
        self.assertEqual(stream.read_size, 101)
        with patch.object(media_library, "MAX_IMAGE_PIXELS", 50):
            with self.assertRaisesRegex(ValueError, "megapixel"):
                self.upload(size=(10, 10))
        with patch.object(media_library, "MAX_DECODED_BYTES", 399):
            with self.assertRaisesRegex(ValueError, "memory limit"):
                self.upload(size=(10, 10))

    def test_missing_or_escaping_ids_and_invalid_edits_are_rejected(self):
        item = self.upload()
        for image_id in ("../index", "../" + item["id"], "A" * 32, "0" * 32, None):
            for operation in (self.library.get, self.library.path, self.library.delete):
                with self.subTest(image_id=image_id, operation=operation.__name__):
                    with self.assertRaises(KeyError):
                        operation(image_id)
        for name in ("", " " * 3, "x" * 121, None):
            with self.assertRaises(ValueError):
                self.library.update(item["id"], name)
        with self.assertRaises(ValueError):
            self.library.update(item["id"], "Name", preset="false")
        for width, height in ((0, 10), (10, -1), (True, 10), (1.5, 10), (10000, 10000)):
            with self.assertRaises(ValueError):
                self.library.frame(item["id"], width, height)

    def test_failed_index_commit_rolls_back_uploaded_files_and_delete(self):
        item = self.upload()
        before = self.library.list()
        with patch.object(self.library, "_save", side_effect=OSError("Disk full")):
            with self.assertRaises(OSError):
                self.upload("Cannot save")
            with self.assertRaises(OSError):
                self.library.delete(item["id"])
        self.assertEqual(self.library.list(), before)
        self.assertTrue(self.library.path(item["id"]).exists())
        self.assertTrue(self.library.path(item["id"], True).exists())
        self.assertEqual(len(list((self.root / "images").iterdir())), 1)
        self.assertEqual(len(list((self.root / "thumbnails").iterdir())), 1)

    def test_failed_thumbnail_write_does_not_leave_partial_uploads(self):
        original_write = media_library._atomic_write

        def fail_thumbnail(path, data):
            if path.parent.name == "thumbnails":
                raise OSError("Simulated write failure")
            return original_write(path, data)

        with patch.object(media_library, "_atomic_write", side_effect=fail_thumbnail):
            with self.assertRaises(OSError):
                self.upload()
        self.assertEqual(self.library.list(), [])
        self.assertEqual(list((self.root / "images").iterdir()), [])

    def test_delete_removes_images_and_metadata_and_corrupt_index_fails_closed(self):
        item = self.upload()
        self.assertEqual(self.library.delete(item["id"]), item)
        self.assertEqual(self.library.list(), [])
        self.assertEqual(list((self.root / "images").iterdir()), [])
        self.assertEqual(list((self.root / "thumbnails").iterdir()), [])
        index = self.root / "index.json"
        index.write_text("{broken", encoding="utf-8")
        with self.assertRaises(OSError):
            self.upload()
        self.assertEqual(index.read_text(encoding="utf-8"), "{broken")
        self.assertEqual(list((self.root / "images").iterdir()), [])

    def test_two_instances_serialize_concurrent_catalogue_updates(self):
        another = MediaLibrary(self.root)
        content = image_bytes()

        def upload(index):
            library = another if index % 2 else self.library
            return library.upload(io.BytesIO(content), f"Image {index}.png")

        with ThreadPoolExecutor(max_workers=4) as executor:
            items = list(executor.map(upload, range(12)))
        self.assertEqual({item["id"] for item in self.library.list()}, {item["id"] for item in items})
        self.assertEqual(len(json.loads((self.root / "index.json").read_text())["images"]), 12)

    def test_frame_reads_cannot_race_library_deletion(self):
        item = self.upload()
        reading = threading.Event()
        release = threading.Event()
        delete_attempted = threading.Event()
        original_open = Image.open

        def paused_open(*args, **kwargs):
            reading.set()
            self.assertTrue(release.wait(3))
            return original_open(*args, **kwargs)

        def delete():
            delete_attempted.set()
            return self.library.delete(item["id"])

        with ThreadPoolExecutor(max_workers=2) as executor:
            with patch.object(media_library.Image, "open", side_effect=paused_open):
                rendering = executor.submit(self.library.frame, item["id"], 8, 4)
                self.assertTrue(reading.wait(3))
                deleting = executor.submit(delete)
                self.assertTrue(delete_attempted.wait(3))
                try:
                    self.assertFalse(deleting.done())
                finally:
                    release.set()
                self.assertEqual(len(rendering.result(timeout=3)), 8 * 4 * 4)
                self.assertEqual(deleting.result(timeout=3)["id"], item["id"])


if __name__ == "__main__":
    unittest.main()
