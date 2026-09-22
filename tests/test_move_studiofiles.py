"""Tests for the pure planning logic of move_studiofiles.py."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import move_studiofiles as ms  # noqa: E402

MINI = ms.MINI_RAW_DIR


def plan_for(lines, existing=()):
    existing = set(existing)
    return ms.build_plan(lines, local_exists=lambda p: p in existing)


def test_mismatched_raw_file_is_moved_server_side_and_recounted():
    lines = [
        "2024-11-08/", "2024-11-08/raw/", "2024-11-08/raw/B20241105_5684.MP4",
        "2024-11-08/raw/L20241108_0001.MP4",
        "2024-11-05/", "2024-11-05/raw/",
    ]
    plan = plan_for(lines)
    assert plan.moves == [("2024-11-08/raw/B20241105_5684.MP4", "2024-11-05/raw/B20241105_5684.MP4")]
    # moved-out file no longer counted in source; counted in target
    assert plan.counts["2024-11-08"] == [1, 0, 0, 0, 0]
    assert plan.counts["2024-11-05"] == [0, 0, 0, 0, 1]


def test_matching_raw_files_are_counted_by_prefix_and_not_moved():
    lines = ["2024-11-13/", "2024-11-13/raw/"] + [
        f"2024-11-13/raw/{p}20241113_{i}.MP4" for i, p in enumerate("LLMRAB")
    ]
    plan = plan_for(lines)
    assert plan.moves == []
    assert plan.counts["2024-11-13"] == [2, 1, 1, 1, 1]


def test_raw_file_without_valid_date_name_is_skipped_with_warning():
    lines = ["2024-11-07/", "2024-11-07/raw/", "2024-11-07/raw/skipped_files.log"]
    plan = plan_for(lines)
    assert plan.moves == []
    assert plan.counts["2024-11-07"] == [0, 0, 0, 0, 0]
    assert any("skipped_files.log" in w for w in plan.warnings)


def test_converted_and_thumbnails_copied_to_mini_with_lowercase_extension():
    lines = [
        "2025-10-27/", "2025-10-27/raw/", "2025-10-27/converted/", "2025-10-27/thumbnails/",
        "2025-10-27/converted/A20251027_1.MP4",
        "2025-10-27/converted/B20251027_2.MP4",
        "2025-10-27/thumbnails/A20251027_1.jpg",
    ]
    plan = plan_for(lines, existing=[f"{MINI}/B20251027_2.mp4"])
    assert plan.copies == [
        ("2025-10-27/converted/A20251027_1.MP4", f"{MINI}/A20251027_1.mp4"),
        ("2025-10-27/thumbnails/A20251027_1.jpg", f"{MINI}/A20251027_1.jpg"),
    ]


def test_date_folder_without_raw_is_skipped_entirely():
    lines = ["2024-01-01/", "2024-01-01/converted/", "2024-01-01/converted/A20240101_1.MP4"]
    plan = plan_for(lines)
    assert "2024-01-01" not in plan.counts
    assert plan.copies == []
    assert any("2024-01-01" in w for w in plan.warnings)


def test_non_date_folders_and_deeper_paths_are_ignored():
    lines = [
        "annotation-tool/", "annotation-tool/raw/", "annotation-tool/raw/B20240101_1.MP4",
        "2024-11-08/", "2024-11-08/raw/", "2024-11-08/raw/sub/", "2024-11-08/raw/sub/B20240101_1.MP4",
        "2024-11-08/raw/AB/",
    ]
    plan = plan_for(lines)
    assert plan.moves == []
    assert plan.counts == {"2024-11-08": [0, 0, 0, 0, 0]}


def test_move_to_same_location_is_not_planned():
    # date in name matches the folder in compact form -> no move
    lines = ["2024-11-08/", "2024-11-08/raw/", "2024-11-08/raw/a20241108_5.mp4"]
    plan = plan_for(lines)
    assert plan.moves == []
    assert plan.counts["2024-11-08"] == [0, 0, 0, 0, 0]  # lowercase 'a' is not counted, as before
