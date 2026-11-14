"""fix-c #12: BOM designator ranges expand; header row is found within the first 10 rows."""
from fluxplace import fabxcheck as FX


def test_ranges_expand(tmp_path):
    p = tmp_path / "bom.csv"
    p.write_text('Comment,Designator\n"10k","R1-R3, C10-C11"\n')
    assert sorted(FX.bom_refs(str(p))) == ["C10", "C11", "R1", "R2", "R3"]


def test_header_below_row_zero(tmp_path):
    p = tmp_path / "bom.csv"
    p.write_text('Title: board rev A\n\nGenerated 2026\nComment,Designator\n"10k","R1"\n')
    assert FX.bom_refs(str(p)) == ["R1"]


def test_mixed_prefix_range_is_not_expanded_across_prefixes(tmp_path):
    p = tmp_path / "bom.csv"
    p.write_text('Designator\n"R1-C3"\n')
    assert FX.bom_refs(str(p)) == ["R1-C3"]
