from quote_collector import fetch_quotes

class Response:
    def raise_for_status(self): pass
    def json(self):
        return {"quotes":[{
          "symbol":"NIFTY","expiry":"2026-11-26","strike":26000,
          "option_type":"CE","bid":100,"ask":101,
          "timestamp":"2026-11-18T10:00:00+05:30","lot_size":65
        }]}

class Session:
    def get(self,*args,**kwargs):
        self.args=args; self.kwargs=kwargs
        return Response()

def test_normalizes_provider_payload(monkeypatch):
    monkeypatch.setenv("QUOTE_PROVIDER_URL","https://provider.example/quotes")
    monkeypatch.setenv("QUOTE_PROVIDER_TOKEN","dummy")
    s=Session()
    result=fetch_quotes(s)
    assert len(result)==1
    assert result[0]["source"]=="configured_provider"
    assert s.kwargs["headers"]["Authorization"]=="Bearer dummy"

def test_requires_explicit_provider(monkeypatch):
    monkeypatch.delenv("QUOTE_PROVIDER_URL",raising=False)
    import pytest
    with pytest.raises(RuntimeError,match="not configured"):
        fetch_quotes()
