# Regras oficiais da SIGTAP (competência 2024-12) usadas para validar um pedido
# cirúrgico. O LLM só extrai os campos; quem decide se o pedido está apto é este
# módulo, de forma determinística e auditável.

import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from rapidfuzz import fuzz, process

SIGTAP_PATH = Path(__file__).resolve().parent.parent / 'data' / 'sigtap'
GRUPO_CIRURGICO = '04'
SEM_LIMITE_IDADE = 9999


@dataclass(frozen=True)
class Procedimento:
    codigo: str
    nome: str
    idade_min_meses: int
    idade_max_meses: int
    cids_compativeis: frozenset


@dataclass
class Validacao:
    pendencias: list = field(default_factory=list)

    @property
    def apto(self):
        return not self.pendencias


def _linhas(arquivo):
    return (SIGTAP_PATH / arquivo).read_text(encoding='latin-1').splitlines()


def normalizar(texto):
    sem_acento = unicodedata.normalize('NFKD', texto).encode('ascii', 'ignore').decode()
    return ' '.join(sem_acento.lower().split())


def normalizar_cid(cid):
    if not cid:
        return None
    cid = cid.upper().replace('.', '').replace(' ', '')
    return cid or None


@lru_cache(maxsize=1)
def procedimentos():
    cids = {}
    for l in _linhas('rl_procedimento_cid.txt'):
        cids.setdefault(l[0:10], set()).add(l[10:14].strip())
    return {
        l[0:10]: Procedimento(
            codigo=l[0:10],
            nome=l[10:260].strip(),
            idade_min_meses=int(l[274:278]),
            idade_max_meses=int(l[278:282]),
            cids_compativeis=frozenset(cids.get(l[0:10], ())),
        )
        for l in _linhas('tb_procedimento.txt')
    }


@lru_cache(maxsize=1)
def cids():
    return {l[0:4].strip(): l[4:104].strip() for l in _linhas('tb_cid.txt')}


@lru_cache(maxsize=1)
def _indice_cirurgico():
    cirurgicos = [p for p in procedimentos().values() if p.codigo.startswith(GRUPO_CIRURGICO)]
    return [normalizar(p.nome) for p in cirurgicos], [p.codigo for p in cirurgicos]


def candidatos(texto_procedimento, n=15):
    """Procedimentos cirúrgicos da SIGTAP com nome mais parecido com o texto do pedido."""
    nomes, codigos = _indice_cirurgico()
    achados = process.extract(normalizar(texto_procedimento), nomes, scorer=fuzz.token_set_ratio, limit=n)
    return [procedimentos()[codigos[i]] for _, _, i in achados]


def validar(codigo_procedimento, cid, idade_anos):
    """Checa o pedido contra a SIGTAP e retorna as pendências."""
    v = Validacao()
    proc = procedimentos().get(codigo_procedimento or '')
    cid = normalizar_cid(cid)

    if proc is None:
        v.pendencias.append('procedimento_nao_identificado')
    if cid is None:
        v.pendencias.append('cid_ausente')
    elif proc is not None and cid not in proc.cids_compativeis:
        v.pendencias.append('cid_incompativel')

    # A SIGTAP 2024-12 traz TP_SEXO = 'I' em todos os CIDs, então não há regra
    # oficial de sexo × CID para checar aqui.
    if proc is not None and idade_anos is not None:
        meses = idade_anos * 12
        fora = meses < proc.idade_min_meses or (proc.idade_max_meses != SEM_LIMITE_IDADE and meses > proc.idade_max_meses)
        if fora:
            v.pendencias.append('idade_fora_da_faixa')
    return v
