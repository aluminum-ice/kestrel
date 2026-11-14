"""Face packs (eink_face 1.2): the face drawn from a picture pack instead of
text, through each e-ink plugin's terminal-only ``face_pack`` option. The
library resolves the option (the built-in ``kestrel`` pack, or a folder of PNGs
under /etc/kestrel/faces/), the plugins paste the picture where the text face
was. PIL is on the test host, so drawing runs for real; nothing touches
hardware or the real /etc."""

import importlib.util
import os
import types

import pytest

from kestrel_pi.capture import CaptureInterface
from kestrel_pi.context import PluginContext, ServiceRegistry
from kestrel_pi.declarations import read_declarations
from kestrel_pi.ui import StubUI, UIState

Image = pytest.importorskip("PIL.Image")
ImageDraw = pytest.importorskip("PIL.ImageDraw")

PLUGIN_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "plugins"))
PANELS = {  # plugin name -> (class name, face box)
    "eink-ws27": ("EinkWs27", (130, 46)),
    "eink-ws213": ("EinkWs213", (88, 35)),
}


def _mod(name):
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_") + "_packs_under_test", os.path.join(PLUGIN_DIR, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MODS = {name: _mod(name) for name in PANELS}
FACE = MODS["eink-ws27"].face


def _plugin(name, tmp_path, options=None):
    mod = MODS[name]
    p = getattr(mod, PANELS[name][0])()
    p.options = options or {}
    p.context = PluginContext(
        name=name, options=options or {}, capture=CaptureInterface(str(tmp_path)),
        connectivity=types.SimpleNamespace(online=False),
        services=ServiceRegistry(), capabilities=("ui", "web"))
    p._setup()
    p._started -= 3600              # past the fingerprint window
    p._addresses = lambda: []
    return p


def _png(path, size=(60, 30), mark=(10, 5, 50, 25), mode="RGBA"):
    """A pack file: a black rectangle on a transparent (or white) page."""
    img = Image.new(mode, size, (0, 0, 0, 0) if mode == "RGBA" else 255)
    ImageDraw.Draw(img).rectangle(mark, fill=(0, 0, 0, 255) if mode == "RGBA" else 0)
    img.save(path, "PNG")
    return path


def _ink(img):
    return sum(1 for px in img.getdata() if px == 0)


# --- the library ---------------------------------------------------------------
def test_the_kestrel_pack_has_every_face_and_they_differ():
    assert set(FACE.KESTREL_PACK) == set(FACE.FACES)
    for _name, (_cls, box) in PANELS.items():
        drawn = {}
        for mood in FACE.FACES:
            img = FACE.draw_pack_face("kestrel", mood, box)
            assert img.mode == "1"
            assert img.width <= box[0] and img.height <= box[1]
            assert _ink(img) > 20, mood
            drawn[mood] = img.tobytes()
        # every face is its own picture
        assert len(set(drawn.values())) == len(drawn)


def test_an_empty_name_keeps_the_text_faces():
    pack = FACE.FacePack("")
    assert not pack.active and pack.label == "text faces"
    assert pack.image("happy", (130, 46)) is None


def test_the_built_in_pack_needs_no_folder(tmp_path):
    pack = FACE.FacePack("kestrel", pack_dir=str(tmp_path / "nowhere"))
    assert pack.active and pack.label == "kestrel (built in)"
    assert pack.image("cool", (130, 46)) is not None


@pytest.mark.parametrize("name", ["../etc", "Kestrel", "my pack", "a/b", ".hidden",
                                  "x" * 33, "-lead"])
def test_a_name_that_could_point_elsewhere_is_refused(tmp_path, name):
    pack = FACE.FacePack(name, pack_dir=str(tmp_path))
    assert not pack.active
    assert "isn't a pack name" in pack.label
    assert pack.image("happy", (130, 46)) is None


def test_a_missing_folder_says_so(tmp_path):
    pack = FACE.FacePack("mine", pack_dir=str(tmp_path))
    assert not pack.active
    assert "no folder" in pack.label and "using text faces" in pack.label


def test_a_folder_pack_draws_its_pictures_fitted_to_the_box(tmp_path):
    (tmp_path / "mine").mkdir()
    _png(tmp_path / "mine" / "happy.png", size=(300, 100), mark=(0, 0, 299, 99))
    pack = FACE.FacePack("mine", pack_dir=str(tmp_path))
    assert pack.active and pack.label == "mine"
    img = pack.image("happy", (130, 46))
    # 3:1 scaled to fit 130 x 46: the width binds
    assert img.mode == "1" and img.size == (130, 43)
    assert _ink(img) == 130 * 43
    # a face the pack doesn't have: the plugin draws the text face
    assert pack.image("sad", (130, 46)) is None


def test_transparency_counts_as_white(tmp_path):
    (tmp_path / "mine").mkdir()
    _png(tmp_path / "mine" / "happy.png", size=(60, 30), mark=(0, 0, 29, 29))
    img = FACE.FacePack("mine", pack_dir=str(tmp_path)).image("happy", (60, 30))
    assert img.size == (60, 30)
    assert 0 < _ink(img) < 60 * 30
    assert img.getpixel((50, 15)) == 255


def test_kestrels_own_moods_fall_back_to_the_nearest_classic_face(tmp_path):
    (tmp_path / "mine").mkdir()
    _png(tmp_path / "mine" / "sleep2.png")
    pack = FACE.FacePack("mine", pack_dir=str(tmp_path))
    assert FACE.PACK_FALLBACK["tired"] == "sleep2"
    assert pack.image("tired", (130, 46)) is not None
    assert pack.image("levelup", (130, 46)) is None


def test_files_that_arent_small_regular_pngs_are_skipped(tmp_path, monkeypatch):
    folder = tmp_path / "mine"
    folder.mkdir()
    # not a PNG (a JPEG with the right name), and junk bytes
    Image.new("RGB", (20, 10), "white").save(folder / "happy.png", "JPEG")
    (folder / "sad.png").write_bytes(b"not an image at all")
    # a symlink to a perfectly good PNG
    _png(tmp_path / "real.png")
    os.symlink(tmp_path / "real.png", folder / "cool.png")
    # a folder named like a face
    (folder / "bored.png").mkdir()
    pack = FACE.FacePack("mine", pack_dir=str(tmp_path))
    for mood in ("happy", "sad", "cool", "bored"):
        assert pack.image(mood, (130, 46)) is None, mood
    # too many bytes, or too many pixels, for the limits
    _png(folder / "angry.png")
    _png(folder / "smart.png", size=(64, 32), mark=(0, 0, 10, 10))
    monkeypatch.setattr(FACE, "PACK_MAX_BYTES", 40)
    assert FACE.load_pack_png(str(folder / "angry.png"), (130, 46)) is None
    monkeypatch.setattr(FACE, "PACK_MAX_BYTES", 512 * 1024)
    monkeypatch.setattr(FACE, "PACK_MAX_SIDE", 32)
    assert FACE.load_pack_png(str(folder / "smart.png"), (130, 46)) is None
    monkeypatch.setattr(FACE, "PACK_MAX_SIDE", 2048)
    assert FACE.load_pack_png(str(folder / "smart.png"), (130, 46)) is not None


def test_pictures_are_cached_per_mood_and_box(tmp_path):
    (tmp_path / "mine").mkdir()
    path = _png(tmp_path / "mine" / "happy.png")
    pack = FACE.FacePack("mine", pack_dir=str(tmp_path))
    first = pack.image("happy", (130, 46))
    os.remove(path)
    assert pack.image("happy", (130, 46)) is first
    assert pack.image("happy", (88, 35)) is None


# --- the plugins --------------------------------------------------------------
@pytest.mark.parametrize("name", sorted(PANELS))
def test_face_pack_is_a_terminal_only_option(name):
    opts = read_declarations(os.path.join(PLUGIN_DIR, name + ".py"))[name]["options"]
    spec = opts["face_pack"]
    assert spec["type"] == "str" and spec["default"] == ""
    assert spec.get("root_only") is True


@pytest.mark.parametrize("name", sorted(PANELS))
def test_the_panel_draws_the_pack_where_the_text_face_was(tmp_path, name):
    text = _plugin(name, tmp_path)._compose_image(UIState(), "cool", "")
    packed = _plugin(name, tmp_path, {"face_pack": "kestrel"})._compose_image(
        UIState(), "cool", "")
    assert text.size == packed.size
    box = PANELS[name][1]
    left = 10 if name == "eink-ws27" else 5
    top = 30 if name == "eink-ws27" else 18
    region = (left, top, left + box[0], top + box[1])
    assert text.crop(region).tobytes() != packed.crop(region).tobytes()
    # the rest of the frame (the status rows and below) is unchanged
    below = (0, top + box[1] + 1, packed.width, packed.height)
    assert text.crop(below).tobytes() == packed.crop(below).tobytes()


@pytest.mark.parametrize("name", sorted(PANELS))
def test_a_face_the_pack_lacks_is_drawn_as_text(tmp_path, name, monkeypatch):
    monkeypatch.setattr(FACE, "PACK_DIR", str(tmp_path))
    (tmp_path / "mine").mkdir()
    _png(tmp_path / "mine" / "happy.png")
    text = _plugin(name, tmp_path)._compose_image(UIState(), "sad", "")
    packed = _plugin(name, tmp_path, {"face_pack": "mine"})._compose_image(UIState(), "sad", "")
    assert text.tobytes() == packed.tobytes()


@pytest.mark.parametrize("name", sorted(PANELS))
def test_the_card_names_the_pack_only_when_one_is_set(tmp_path, name, monkeypatch):
    monkeypatch.setattr(FACE, "PACK_DIR", str(tmp_path))
    for options, want in (({}, None), ({"face_pack": "kestrel"}, "kestrel (built in)"),
                          ({"face_pack": "gone"}, "no folder")):
        p = _plugin(name, tmp_path, options)
        ui = StubUI()
        view = ui.for_plugin(name)
        p.on_ui_setup(view)
        card = {e["name"]: e for e in ui.widgets().get(name, [])}
        if want is None:
            assert "face_pack" not in card
        else:
            assert want in card["face_pack"]["value"]
