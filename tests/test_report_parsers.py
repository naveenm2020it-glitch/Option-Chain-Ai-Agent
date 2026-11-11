from nifty_ai.reports import load_tables


def test_participant_volume_ingestion_skips_title_row(tmp_path):
    path = tmp_path / "fao_participant_vol_09112026.csv"
    path.write_bytes(
        b'""Participant wise Trading Volume as on Nov 09, 2026"",,,,,\n'
        b"Client Type,Future Index Long,Future Index Short,Total Long Contracts,Total Short Contracts\n"
        b"Client,100,90,200,190\nDII,20,10,30,20\nFII,50,40,90,80\nPro,30,50,80,100\nTOTAL,200,200,400,400\n"
    )
    tables = load_tables(str(path))
    assert len(tables) == 1
    frame = tables[0][1]
    assert len(frame) == 5
    assert "Client Type" in frame.columns
    assert "Future Index Long" in frame.columns
