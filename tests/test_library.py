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


def test_image_size_limit_and_metadata_validation(image_path, monkeypatch):
    monkeypatch.setattr(library, "IMAGE_MAX_BYTES", 10)
    with pytest.raises(ValueError, match="上限"):
        library.register_image(image_path)
    with pytest.raises(ValueError, match="metadata"):
        library.register_image(image_path, metadata=[])
