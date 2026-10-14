"""缩略图身份只取图片像素，排除随滚动位置变化的右侧滚动栏。"""

from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from channel.wechat_desktop.uia.chat_record_reader import WechatChatRecordReader
from channel.wechat_desktop.uia.client import WechatUiaClient
from .helpers import GeometryControl


WINDOW_BOUNDS = (100, 200, 847, 908)
VIEWPORT_BOUNDS = WINDOW_BOUNDS
VIEWPORT_SIZE = (747, 708)
GUTTER_SETTING = "chat_record_uia_thumbnail_right_gutter_pixels"


def _reader(config=None):
    # 只调用几何 helper；不构造或连接真实微信窗口。
    client = SimpleNamespace(
        config=dict(config or {}),
        _image_activation_bounds=WechatUiaClient._image_activation_bounds,
    )
    return WechatChatRecordReader(client)


def _image_row(top, height, children=None):
    row = GeometryControl(
        (100, top, 847, top + height),
        "Synthetic 图片",
        "mmui::ChatBubbleReferItemView",
        children=children,
    )
    row.GetRuntimeId = lambda: (42, 20)
    return row


def _page(row, thumb_top, *, gutter_width=20):
    """固定相对行坐标的图片纹理，仅滚动栏使用页面坐标。"""
    bounds = row.BoundingRectangle
    height = bounds.bottom - bounds.top
    sprite = Image.new("RGB", (VIEWPORT_SIZE[0], height), (235, 235, 235))
    image_draw = ImageDraw.Draw(sprite)
    image_draw.rectangle((0, 0, VIEWPORT_SIZE[0] - gutter_width - 1, height - 1),
                         fill=(32, 80, 160))
    image_draw.rectangle((15, 10, 120, min(75, height - 1)), fill=(192, 128, 24))
    image_draw.line((10, height - 10, 700, 15), fill=(24, 200, 160), width=3)
    page = Image.new("RGB", VIEWPORT_SIZE, (250, 250, 250))
    page.paste(sprite, (bounds.left - WINDOW_BOUNDS[0], bounds.top - WINDOW_BOUNDS[1]))
    page_draw = ImageDraw.Draw(page)
    page_draw.rectangle((VIEWPORT_SIZE[0] - gutter_width, 0,
                         VIEWPORT_SIZE[0] - 1, VIEWPORT_SIZE[1] - 1),
                        fill=(242, 242, 242))
    page_draw.rectangle((VIEWPORT_SIZE[0] - gutter_width, thumb_top,
                         VIEWPORT_SIZE[0] - 1, thumb_top + 65),
                        fill=(110, 110, 110))
    return page


def _visual_key(reader, row, page):
    return reader._image_visual_key(row, page, WINDOW_BOUNDS, VIEWPORT_BOUNDS)


@pytest.mark.parametrize("height", [375, 205])
def test_scrollbar_thumb_movement_does_not_change_full_row_image_identity(height):
    reader = _reader()
    row = _image_row(270, height)
    first = _page(row, 90)
    changed = _page(row, 420)

    assert first.tobytes() != changed.tobytes()
    assert _visual_key(reader, row, first) == _visual_key(reader, row, changed)


@pytest.mark.parametrize("height", [375, 205])
def test_same_fully_visible_image_keeps_identity_after_row_moves_up(height):
    reader = _reader()
    before = _image_row(300, height)
    after = _image_row(230, height)
    first = _visual_key(reader, before, _page(before, 150))
    moved = _visual_key(reader, after, _page(after, 450))

    assert first == moved
    assert first[2] is True


@pytest.mark.parametrize("height", [375, 205])
def test_single_changed_image_pixel_produces_a_different_identity(height):
    reader = _reader()
    row = _image_row(270, height)
    first = _page(row, 90)
    changed = first.copy()
    changed.putpixel((25, row.BoundingRectangle.top - WINDOW_BOUNDS[1] + height // 2),
                     (32, 80, 159))

    assert _visual_key(reader, row, first) != _visual_key(reader, row, changed)


@pytest.mark.parametrize("height", [375, 205])
def test_excluding_scrollbar_pixels_does_not_mark_a_full_image_as_partial(height):
    row = _image_row(270, height)
    key = _visual_key(_reader(), row, _page(row, 90))

    assert key[:3] == (727, height, True)


@pytest.mark.parametrize("height", [375, 205])
@pytest.mark.parametrize("clipped_edge", ["top", "bottom"])
def test_top_or_bottom_clipping_still_marks_the_image_as_partial(height, clipped_edge):
    top = (VIEWPORT_BOUNDS[1] - height // 2 if clipped_edge == "top"
           else VIEWPORT_BOUNDS[3] - height // 2)
    row = _image_row(top, height)
    key = _visual_key(_reader(), row, _page(row, 90))

    assert key[0] == 727
    assert 0 < key[1] < height
    assert key[2] is False


def test_inset_image_child_keeps_its_full_width_and_last_pixel():
    image = GeometryControl((130, 290, 340, 450), "图片", "mmui::ChatImageItemView")
    row = _image_row(270, 375, children=[image])
    page = _page(row, 90)
    reader = _reader()
    key = _visual_key(reader, row, page)

    assert key[:3] == (210, 160, True)
    assert key == _visual_key(_reader({GUTTER_SETTING: 0}), row, page)
    changed = page.copy()
    changed.putpixel((image.BoundingRectangle.right - WINDOW_BOUNDS[0] - 1,
                      image.BoundingRectangle.top - WINDOW_BOUNDS[1] + 10),
                     (32, 80, 159))
    assert key != _visual_key(reader, row, changed)
    assert key == _visual_key(reader, row, _page(row, 420))


@pytest.mark.parametrize("configured_gutter", [0, -5])
def test_zero_or_negative_setting_disables_scrollbar_exclusion(configured_gutter):
    reader = _reader({GUTTER_SETTING: configured_gutter})
    row = _image_row(270, 375)
    first = _visual_key(reader, row, _page(row, 90))
    changed = _visual_key(reader, row, _page(row, 420))

    assert first[:3] == (747, 375, True)
    assert first != changed


def test_oversized_setting_is_limited_to_24_pixels():
    row = _image_row(270, 375)
    first = _page(row, 90, gutter_width=24)
    changed = _page(row, 420, gutter_width=24)
    reader = _reader({GUTTER_SETTING: 999})
    key = _visual_key(reader, row, first)

    assert key[:3] == (723, 375, True)
    assert key == _visual_key(reader, row, changed)
    assert key == _visual_key(_reader({GUTTER_SETTING: 24}), row, first)
