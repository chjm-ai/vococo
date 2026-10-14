"""作品索引、去重、原件保留与登记幂等。"""
from concurrent.futures import ThreadPoolExecutor

from PIL import Image
import pytest

from vococo import config
from vococo.memory import agents, library


@pytest.fixture
def image_path(isolated):
    path = isolated / "output.png"
    Image.new("RGB", (4, 4), "red").save(path)
    return path


def test_generated_and_sent_share_original_and_keep_fields(image_path, monkeypatch):
    monkeypatch.setattr(agents, "agent_for_session", lambda key: {"id": key})
    first = library.register_image(image_path, "原标题", "a", 11, "generated",
                                   {"prompt": "画红色"}, "generation-1")
    library.update_item(first["id"], title="自定义", pinned=True, hidden=True)
    second = library.register_image(image_path, "另一标题", "b", 12, event_key="send-1")
    assert second["id"] == first["id"]
    assert (second["agent_id"], second["title"], second["pinned"], second["hidden"]) == (
        "a", "自定义", True, True)
    assert [source["action"] for source in second["sources"]] == ["generated", "sent"]
    assert [source["turn_id"] for source in second["sources"]] == [11, 12]
    assert second["sources"][0]["metadata"] == {"prompt": "画红色"}
    data = image_path.read_bytes()
    image_path.unlink()
    assert library.image_file(first["id"]).read_bytes() == data
    assert len(list((config.DATA_DIR / "library/images").iterdir())) == 1
    assert library.list_items() == []
    assert library.list_items(state="hidden")[0]["id"] == first["id"]


def test_retry_event_is_idempotent_and_conflicts_are_explicit(image_path):
    first = library.register_image(image_path, event_key="same")
    retry = library.register_image(image_path, event_key="same")
    assert retry == first
    with pytest.raises(ValueError, match="event_key"):
        library.register_image(image_path, action="generated", event_key="same")
    with pytest.raises(ValueError, match="event_key"):
        library.register_website("https://example.com", "站点", "x", event_key="same")


def test_concurrent_registration_is_one_item_one_event(image_path):
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda _: library.register_image(image_path, event_key="parallel"), range(8)))
    assert len({row["id"] for row in rows}) == 1
    assert len(library.get_item(rows[0]["id"])["sources"]) == 1


def test_site_identity_ignores_agent_and_updates_url(isolated, monkeypatch):
    monkeypatch.setattr(agents, "agent_for_session", lambda key: {"id": key})
    first = library.register_website("https://example.com/v1", "标题", "target:app", "a", 1)
    library.update_item(first["id"], pinned=True, hidden=True)
    second = library.register_website("https://example.com/v2", "新标题", "target:app", "b", 2)
    assert second["id"] == first["id"]
    assert second["url"] == "https://example.com/v2"
    assert second["title"] == "标题" and second["agent_id"] == "a"
    assert second["pinned"] and second["hidden"]
    assert len(second["sources"]) == 2


def test_pinned_filter_excludes_hidden_and_unpinned(isolated):
    pinned = library.register_website("https://example.com/a", "置顶", "a")
    hidden = library.register_website("https://example.com/b", "隐藏置顶", "b")
    library.register_website("https://example.com/c", "未置顶", "c")
    library.update_item(pinned["id"], pinned=True)
    library.update_item(hidden["id"], pinned=True, hidden=True)
    assert [row["id"] for row in library.list_items(state="pinned")] == [pinned["id"]]
    assert library.list_items(state="pinned", search="隐藏") == []


def test_unknown_source_does_not_guess_owner(image_path):
    row = library.register_image(image_path)
    assert row["agent_id"] == ""
    assert row["sources"][0]["session_key"] == ""
    assert row["sources"][0]["turn_id"] is None


def test_list_search_pagination_and_pin_order(isolated):
    first = library.register_website("https://example.com/a", "Alpha", "a")
    second = library.register_website("https://example.com/b", "Beta", "b")
    library.update_item(first["id"], pinned=True)
    assert [row["id"] for row in library.list_items(limit=1)] == [first["id"]]
    assert library.list_items(limit=1, offset=1)[0]["id"] == second["id"]
    assert library.list_items(search="alpha")[0]["id"] == first["id"]
    assert library.list_items(kind="image") == []
    assert library.get_item("unknown") is None


@pytest.mark.parametrize("fields", [{"url": "https://evil.com"}, {"pinned": 1},
                                    {"hidden": "false"}, {"title": None}, {}, {"agent_id": "missing"}])
def test_patch_rejects_invalid_fields(isolated, fields):
    row = library.register_website("https://example.com", "标题", "x")
    with pytest.raises(ValueError):
        library.update_item(row["id"], **fields)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "javascript:alert(1)",
                                 "https://user:pass@example.com", "https://example.com\n", "https://a:bad"])
def test_site_rejects_unsafe_url(isolated, url):
    with pytest.raises(ValueError):
        library.register_website(url, "", "x")


def test_invalid_image_and_missing_original_fail_explicitly(image_path):
    fake = image_path.parent / "fake.png"
    fake.write_bytes(b"not an image")
    with pytest.raises(ValueError, match="无效"):
        library.register_image(fake)
    row = library.register_image(image_path)
    original = library.image_file(row["id"])
    original.unlink()
    with pytest.raises(FileNotFoundError):
        library.image_file(row["id"])
    original.symlink_to(image_path)
    with pytest.raises(ValueError, match="路径"):
        library.image_file(row["id"])


@pytest.mark.parametrize("kwargs", [{"kind": "video"}, {"state": "deleted"},
                                    {"limit": 0}, {"limit": 201}, {"offset": -1}])
def test_invalid_filters_fail(isolated, kwargs):
    with pytest.raises(ValueError):
        library.list_items(**kwargs)


def test_conflicting_event_does_not_leave_an_orphan(image_path):
    first = library.register_image(image_path, event_key="one")
    Image.new("RGB", (4, 4), "blue").save(image_path)
    with pytest.raises(ValueError, match="event_key"):
        library.register_image(image_path, event_key="one")
    files = list((config.DATA_DIR / "library/images").iterdir())
    assert files == [library.image_file(first["id"])]
    assert len(library.list_items()) == 1


def test_registration_error_rolls_back_new_file_and_index(image_path, monkeypatch):
    def fail_item(*args):
        raise RuntimeError("模拟返回结果失败")
    monkeypatch.setattr(library, "_item", fail_item)
    with pytest.raises(RuntimeError):
        library.register_image(image_path)
    assert list((config.DATA_DIR / "library/images").iterdir()) == []
    assert library.list_items() == []


def test_symlink_storage_directory_is_rejected(image_path):
    outside = image_path.parent / "outside"
    outside.mkdir()
    config.DATA_DIR.mkdir()
    (config.DATA_DIR / "library").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="符号链接"):
        library.register_image(image_path)
    assert list(outside.iterdir()) == []
    assert library.list_items() == []


@pytest.mark.parametrize("image_format", ["PNG", "JPEG", "GIF", "WEBP"])
def test_website_cover_is_preserved_without_image_work(image_path, image_format):
    Image.new("RGB", (4, 4), "blue").save(image_path, format=image_format)
    data = image_path.read_bytes()
    row = library.register_website("https://example.com", "网站", "cover-site", cover_path=image_path)
    assert row["cover_url"] == f"/library/cover?id={row['id']}"
    assert row["file_url"] == ""
    image_path.unlink()
    assert library.cover_file(row["id"]).read_bytes() == data
    assert library.list_items(kind="image") == []
    assert len(library.list_items()) == 1
    with pytest.raises(ValueError, match="不是图片"):
        library.image_file(row["id"])


def test_website_cover_updates_and_missing_argument_keeps_cover(image_path):
    first = library.register_website("https://example.com/v1", "原标题", "site", cover_path=image_path)
    old_path = library.cover_file(first["id"])
    library.update_item(first["id"], hidden=True, pinned=True)
    without_cover = library.register_website("https://example.com/v2", "新标题", "site")
    assert without_cover["cover_url"] == first["cover_url"]
    assert library.cover_file(first["id"]) == old_path
    Image.new("RGB", (4, 4), "blue").save(image_path)
    updated = library.register_website("https://example.com/v3", "新标题", "site", cover_path=image_path)
    assert updated["id"] == first["id"] and updated["url"].endswith("v3")
    assert updated["title"] == "原标题" and updated["hidden"] and updated["pinned"]
    assert library.cover_file(first["id"]) != old_path
    assert library.cover_file(first["id"]).read_bytes() == image_path.read_bytes()


def test_failed_website_cover_update_preserves_previous_version(image_path, monkeypatch):
    row = library.register_website("https://example.com/v1", "网站", "site", cover_path=image_path)
    original = library.cover_file(row["id"])
    Image.new("RGB", (4, 4), "blue").save(image_path)
    def fail_item(*args):
        raise RuntimeError("保存失败")
    with monkeypatch.context() as patch:
        patch.setattr(library, "_item", fail_item)
        with pytest.raises(RuntimeError):
            library.register_website("https://example.com/v2", "网站", "site", cover_path=image_path)
    assert library.get_item(row["id"]) == row
    assert list(original.parent.iterdir()) == [original]


def test_bad_website_cover_update_and_event_conflict_leave_no_orphan(image_path):
    row = library.register_website("https://example.com/v1", "网站", "site",
                                   cover_path=image_path, event_key="cover-event")
    original = library.cover_file(row["id"])
    image_path.write_bytes(b"not an image")
    with pytest.raises(ValueError, match="无效"):
        library.register_website("https://example.com/v2", "网站", "site", cover_path=image_path)
    Image.new("RGB", (4, 4), "blue").save(image_path)
    with pytest.raises(ValueError, match="event_key"):
        library.register_website("https://example.com/v2", "网站", "other",
                                 cover_path=image_path, event_key="cover-event")
    assert library.get_item(row["id"]) == row
    assert list(original.parent.iterdir()) == [original]


def test_website_cover_paths_and_storage_are_controlled(image_path):
    link = image_path.parent / "link.png"
    link.symlink_to(image_path)
    for invalid in [link, image_path.parent, image_path.parent / "missing.png"]:
        with pytest.raises(ValueError, match="路径"):
            library.register_website("https://example.com", "网站", "site", cover_path=invalid)
    row = library.register_website("https://example.com", "网站", "site", cover_path=image_path)
    original = library.cover_file(row["id"])
    original.unlink()
    with pytest.raises(FileNotFoundError):
        library.cover_file(row["id"])
    original.symlink_to(image_path)
    with pytest.raises(ValueError, match="路径"):
        library.cover_file(row["id"])
    for key in [str(image_path), "../output.png"]:
        with library._connect() as connection:
            connection.execute("UPDATE library_items SET storage_key=? WHERE id=?", (key, row["id"]))
        with pytest.raises(ValueError, match="路径"):
            library.cover_file(row["id"])


def test_old_website_without_cover_is_compatible(image_path):
    with library._connect() as connection:
        connection.execute(
            "INSERT INTO library_items (id,kind,identity,title,url,created_at,updated_at) "
            "VALUES ('legacy','website','website:old-site','旧网站','https://example.com',1,1)"
        )
    row = library.get_item("legacy")
    assert row["cover_url"] == ""
    assert library.list_items()[0]["cover_url"] == ""
    with pytest.raises(FileNotFoundError, match="没有封面"):
        library.cover_file(row["id"])
    covered = library.register_website("https://example.com", "旧网站", "old-site", cover_path=image_path)
    assert covered["id"] == row["id"] and covered["cover_url"]
    image = library.register_image(image_path)
    with pytest.raises(ValueError, match="不是网站"):
        library.cover_file(image["id"])


def test_concurrent_website_cover_registration_is_idempotent(image_path):
    def register(_):
        return library.register_website("https://example.com", "网站", "site",
                                        cover_path=image_path, event_key="cover-event")
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(register, range(8)))
    assert all(row == rows[0] for row in rows)
    assert len(rows[0]["sources"]) == 1
    assert len(list(library.cover_file(rows[0]["id"]).parent.iterdir())) == 1


def test_website_cover_retry_survives_deleted_source(image_path):
    row = library.register_website("https://example.com", "网站", "site",
                                   cover_path=image_path, event_key="cover-event")
    data = image_path.read_bytes()
    image_path.unlink()
    retry = library.register_website("https://example.com", "网站", "site",
                                    cover_path=image_path, event_key="cover-event")
    assert retry == row
    assert library.cover_file(row["id"]).read_bytes() == data
    with pytest.raises(ValueError, match="event_key"):
        library.register_website("https://example.com", "网站", "different",
                                 cover_path=image_path, event_key="cover-event")


def test_website_cover_rejects_symlink_storage_directory(image_path):
    outside = image_path.parent / "outside"
    outside.mkdir()
    config.DATA_DIR.mkdir()
    (config.DATA_DIR / "library").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="符号链接"):
        library.register_website("https://example.com", "网站", "site", cover_path=image_path)
    assert list(outside.iterdir()) == []
    assert library.list_items() == []


def test_image_size_limit_and_metadata_validation(image_path, monkeypatch):
    monkeypatch.setattr(library, "IMAGE_MAX_BYTES", 10)
    with pytest.raises(ValueError, match="上限"):
        library.register_image(image_path)
    with pytest.raises(ValueError, match="metadata"):
        library.register_image(image_path, metadata=[])


def test_thumbnail_is_small_cached_and_concurrent(image_path, monkeypatch):
    Image.effect_noise((2000, 1400), 80).convert("RGB").save(image_path)
    row = library.register_image(image_path)
    original = library.image_file(row["id"]).read_bytes()
    calls = []
    encode = library._encode_thumbnail
    def counted(path):
        calls.append(path)
        return encode(path)
    monkeypatch.setattr(library, "_encode_thumbnail", counted)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: library.thumbnail_data(row["id"]), range(16)))
    assert len(calls) == 1 and all(result == results[0] for result in results)
    data, version = results[0]
    assert len(data) < len(original) / 10
    import io
    with Image.open(io.BytesIO(data)) as thumb:
        assert thumb.size == (480, 336) and thumb.format == "WEBP"
    assert library.image_file(row["id"]).read_bytes() == original
    assert len(library.list_items()) == 1
    assert row["thumbnail_url"].endswith("&v=" + version)
    monkeypatch.setattr(library, "_read_image", lambda _: pytest.fail("缓存命中不得读取原件"))
    assert library.thumbnail_data(row["id"], version) == results[0]


def test_thumbnail_corrupt_cache_recovers_and_paths_are_controlled(image_path):
    row = library.register_image(image_path)
    expected, version = library.thumbnail_data(row["id"])
    cache = library._thumbnail_directory() / f"{version}.webp"
    cache.write_bytes(b"broken")
    assert library.thumbnail_data(row["id"]) == (expected, version)
    cache.unlink()
    cache.symlink_to(image_path)
    with pytest.raises(ValueError, match="符号链接"):
        library.thumbnail_data(row["id"])
    cache.unlink()
    cache.parent.rmdir()
    cache.parent.symlink_to(image_path.parent, target_is_directory=True)
    with pytest.raises(ValueError, match="符号链接"):
        library.thumbnail_data(row["id"])


def test_thumbnail_cover_versions_and_no_cover(image_path):
    site = library.register_website("https://example.com", "网站", "thumb-site", cover_path=image_path)
    _, first_version = library.thumbnail_data(site["id"])
    Image.new("RGB", (4, 4), "blue").save(image_path)
    updated = library.register_website("https://example.com", "网站", "thumb-site", cover_path=image_path)
    _, second_version = library.thumbnail_data(site["id"])
    assert first_version != second_version and site["thumbnail_url"] != updated["thumbnail_url"]
    with pytest.raises(library.ThumbnailVersionError):
        library.thumbnail_data(site["id"], first_version)
    empty = library.register_website("https://example.com/empty", "无封面", "empty")
    assert empty["thumbnail_url"] == ""
    with pytest.raises(FileNotFoundError):
        library.thumbnail_data(empty["id"])
    assert library.list_items(kind="image") == []


@pytest.mark.parametrize("version", ["", "../escape", "a" * 65, "Z" * 64])
def test_thumbnail_rejects_invalid_version(image_path, version):
    row = library.register_image(image_path)
    with pytest.raises(ValueError, match="版本无效"):
        library.thumbnail_data(row["id"], version)


def test_thumbnail_handles_exif_and_animation_first_frame(image_path):
    import io
    exif = Image.Exif()
    exif[274] = 6
    Image.new("RGB", (900, 600), "red").save(image_path, format="JPEG", exif=exif)
    row = library.register_image(image_path)
    data, _ = library.thumbnail_data(row["id"])
    with Image.open(io.BytesIO(data)) as thumb:
        assert thumb.size == (320, 480)
    Image.new("RGB", (40, 20), "red").save(
        image_path, format="GIF", save_all=True,
        append_images=[Image.new("RGB", (40, 20), "blue")], duration=100, loop=0)
    row = library.register_image(image_path)
    data, _ = library.thumbnail_data(row["id"])
    with Image.open(io.BytesIO(data)) as thumb:
        assert not getattr(thumb, "is_animated", False)
        red, green, blue = thumb.convert("RGB").getpixel((0, 0))
        assert red > 200 and blue < 20


def test_thumbnail_revalidates_changed_original_and_pixel_limit(image_path):
    row = library.register_image(image_path)
    _, version = library.thumbnail_data(row["id"])
    path = library.image_file(row["id"])
    Image.new("RGB", (6400, 6300), "red").save(path)
    assert library.get_item(row["id"])["thumbnail_url"] != row["thumbnail_url"]
    with pytest.raises(library.ThumbnailVersionError):
        library.thumbnail_data(row["id"], version)
    with pytest.raises(ValueError, match="像素"):
        library.thumbnail_data(row["id"])


def test_thumbnail_rejects_source_changed_during_encoding(image_path, monkeypatch):
    row = library.register_image(image_path)
    encode = library._encode_thumbnail
    def change_source(path):
        data = encode(path)
        Image.new("RGB", (5, 5), "blue").save(path)
        return data
    monkeypatch.setattr(library, "_encode_thumbnail", change_source)
    with pytest.raises(library.ThumbnailVersionError):
        library.thumbnail_data(row["id"])
    assert list(library._thumbnail_directory().iterdir()) == []


def test_thumbnail_cache_does_not_bypass_missing_or_symlink_original(image_path):
    row = library.register_image(image_path)
    library.thumbnail_data(row["id"])
    path = library.image_file(row["id"])
    path.unlink()
    with pytest.raises(FileNotFoundError):
        library.thumbnail_data(row["id"])
    path.symlink_to(image_path)
    with pytest.raises(ValueError, match="路径"):
        library.thumbnail_data(row["id"])


def test_thumbnail_transparency_and_small_image_are_preserved(image_path):
    import io
    Image.new("RGBA", (12, 8), (255, 0, 0, 0)).save(image_path)
    row = library.register_image(image_path)
    data, _ = library.thumbnail_data(row["id"])
    with Image.open(io.BytesIO(data)) as thumb:
        assert thumb.size == (12, 8)
        assert thumb.convert("RGBA").getpixel((0, 0))[3] == 0
