import csv, io, json, tempfile, unittest, zipfile
from pathlib import Path
from scripts.normalize_eod_dataset import normalize, num, dte, window_rows

class ParsingTests(unittest.TestCase):
    def test_date_formats(self):
        self.assertEqual(dte("18-11-2026"),"2026-11-18")
        self.assertEqual(dte("2026-11-18"),"2026-11-18")
    def test_missing_numeric_and_commas(self):
        self.assertIsNone(num("-"))
        self.assertEqual(num("1,250.5"),1250.5)

class PipelineTests(unittest.TestCase):
    def fixture(self,path, include_pe=True):
        manifest={"analysis_date":"2026-11-18","selected_expiry":"2026-11-23","sessions":[{"session_date":"2026-11-18","status":"aligned"}]}
        rows=["2026-11-18,2026-11-18,FO,1,NIFTY,2026-11-23,25000,CE,NIFTYCE,100,110,90,105,105,100,25200,105,1000,50,200,21000,10,65"]
        if include_pe: rows.append("2026-11-18,2026-11-18,FO,2,NIFTY,2026-11-23,25000,PE,NIFTYPE,90,100,80,95,95,90,25200,95,1200,-20,250,23750,12,65")
        fo="TradDt,BizDt,Sgmt,FinInstrmId,TckrSymb,XpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,NewBrdLotQty\n"+"\n".join(rows)+"\n"
        idx="Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,Closing Index Value,Points Change,Change(%),Volume,Turnover (Rs. Cr.),P/E,P/B,Div Yield\nNifty 50,18-11-2026,25300,25400,25100,25200,-100,-.4,100000,1000,20,3,1\n"
        with zipfile.ZipFile(path,"w") as z:
            z.writestr("reports/trial-manifest.json",json.dumps(manifest)); b=io.BytesIO()
            with zipfile.ZipFile(b,"w") as inner: inner.writestr("bhav.csv",fo)
            z.writestr("reports/2026-11-18/fo_udiff_bhavcopy.zip",b.getvalue()); z.writestr("reports/2026-11-18/nifty_index_close_all.csv",idx)
    def test_ce_pe_and_index_output(self):
        with tempfile.TemporaryDirectory() as td:
            a=Path(td)/"in.zip"; o=Path(td)/"out"; self.fixture(a); m=normalize(a,o,1)
            self.assertEqual((m["option_contract_rows"],m["index_rows"]),(2,1))
            with (o/"nifty_options_eod.csv").open() as f: rows=list(csv.DictReader(f))
            self.assertEqual({r["option_type"] for r in rows},{"CE","PE"})
            self.assertEqual(rows[0]["lot_size"],"65")
            self.assertTrue((o/"dataset_manifest.json").exists())
    def test_rejects_missing_option_side(self):
        with tempfile.TemporaryDirectory() as td:
            a=Path(td)/"in.zip"; self.fixture(a,False)
            with self.assertRaisesRegex(ValueError,"Missing CE or PE"): normalize(a,Path(td)/"out",1)
    def test_rejects_wrong_session_count(self):
        with tempfile.TemporaryDirectory() as td:
            a=Path(td)/"in.zip"; self.fixture(a)
            with self.assertRaisesRegex(ValueError,"Expected 22 sessions"): normalize(a,Path(td)/"out",22)

if __name__=="__main__": unittest.main()
