"""Toda variável de ambiente lida pelo código aparece no .env.mvp.example (F6)."""
import re
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
_LEITURA = re.compile(r'(?:_get|_int|_float|_bool|os\.getenv|os\.environ\.get)\(\s*"([A-Z][A-Z0-9_]+)"')


def _lidas() -> set:
    nomes = set()
    for arq in (RAIZ / "factcheck_mvp").glob("*.py"):
        nomes |= set(_LEITURA.findall(arq.read_text(encoding="utf-8")))
    return nomes


def test_toda_variavel_lida_esta_no_exemplo():
    exemplo = (RAIZ / ".env.mvp.example").read_text(encoding="utf-8")
    documentadas = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", exemplo, re.M))
    lidas = _lidas()
    assert len(lidas) > 40  # guarda anti-vácuo: a regex achou as leituras
    assert not lidas - documentadas, f"faltam no .env.mvp.example: {sorted(lidas - documentadas)}"


def test_exemplo_nao_tem_chave_preenchida():
    exemplo = (RAIZ / ".env.mvp.example").read_text(encoding="utf-8")
    for nome in ("TELEGRAM_TOKEN", "SERPAPI_KEY", "OPENROUTER_API_KEY"):
        assert re.search(rf"^{nome}=$", exemplo, re.M), nome
