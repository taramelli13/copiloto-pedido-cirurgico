# Gera pedidos cirúrgicos sintéticos em texto livre, com gabarito, a partir das
# regras da SIGTAP. Metade dos pedidos é válida; a outra metade tem um erro
# plantado. O gabarito das pendências vem do próprio motor de regras aplicado
# aos campos verdadeiros, então copiloto e gabarito usam a mesma definição.
#
# Uso:
#   python src/gerar_casos.py 120
#
# Saída: data/casos.jsonl

import json
import random
import sys
from collections import Counter
from pathlib import Path

import sigtap

SEED = 42
SAIDA = Path(__file__).resolve().parent.parent / 'data' / 'casos.jsonl'

# Procedimentos eletivos entre os mais realizados no SUS-SP em 2024, com as
# formas como costumam aparecer em pedidos médicos e a faixa de idade típica.
PROCEDIMENTOS = {
    '0407030034': (['colecistectomia videolaparoscópica', 'colecistectomia por vídeo', 'CCL VLP', 'colecistectomia vídeo-laparoscópica'], (25, 70)),
    '0407030026': (['colecistectomia convencional', 'colecistectomia aberta', 'colecistectomia por laparotomia'], (30, 75)),
    '0405050372': (['facoemulsificação com implante de LIO', 'facectomia por facoemulsificação + LIO dobrável', 'cirurgia de catarata (faco + LIO)'], (55, 85)),
    '0407040102': (['hernioplastia inguinal unilateral', 'herniorrafia inguinal à direita', 'correção de hérnia inguinal esquerda'], (25, 75)),
    '0407040099': (['hernioplastia inguinal bilateral', 'herniorrafia inguinal bilateral', 'correção de hérnia inguinal bilateral'], (30, 75)),
    '0407040129': (['hernioplastia umbilical', 'herniorrafia umbilical', 'correção de hérnia umbilical'], (25, 70)),
    '0407040080': (['hernioplastia incisional', 'correção de hérnia incisional', 'herniorrafia incisional (eventração)'], (35, 75)),
    '0409040240': (['vasectomia', 'esterilização masculina (vasectomia)'], (25, 50)),
    '0409060186': (['laqueadura tubária', 'laqueadura tubária bilateral', 'esterilização feminina (LT)'], (25, 45)),
    '0409050083': (['postectomia', 'circuncisão', 'postectomia por fimose'], (18, 60)),
    '0406020566': (['tratamento cirúrgico de varizes bilateral', 'safenectomia bilateral', 'fleboextração bilateral de varizes'], (30, 70)),
    '0406020574': (['tratamento cirúrgico de varizes unilateral', 'safenectomia em MID', 'fleboextração de varizes em membro inferior esquerdo'], (30, 70)),
    '0404010032': (['amigdalectomia com adenoidectomia', 'adenoamigdalectomia', 'adenotonsilectomia'], (3, 12)),
    '0409060178': (['histeroscopia cirúrgica', 'histeroscopia cirúrgica com ressectoscópio', 'polipectomia histeroscópica'], (30, 65)),
    '0407020284': (['hemorroidectomia', 'hemorroidectomia convencional', 'exérese de mamilos hemorroidários'], (25, 70)),
    '0409060119': (['histerectomia com anexectomia', 'histerectomia total com salpingo-ooforectomia bilateral', 'HTA + anexectomia bilateral'], (40, 70)),
    '0409010596': (['ureterolitotripsia transureteroscópica', 'ureterolitotripsia flexível', 'URS com litotripsia'], (25, 70)),
    '0403020123': (['descompressão do túnel do carpo', 'liberação do nervo mediano no carpo', 'tratamento cirúrgico de síndrome do túnel do carpo'], (30, 70)),
}
SEXO_FIXO = {'0409060186': 'F', '0409060178': 'F', '0409060119': 'F', '0409040240': 'M', '0409050083': 'M'}

ERROS = ['cid_incompativel', 'cid_ausente', 'idade_fora_da_faixa']


def com_limite_de_idade(codigo):
    p = sigtap.procedimentos()[codigo]
    return p.idade_min_meses >= 24 or p.idade_max_meses < 1500


def cid_formatado(cid, rng):
    return cid[:3] + '.' + cid[3:] if len(cid) == 4 and rng.random() < 0.5 else cid


def redigir(sexo, idade, hd, proc_texto, rng):
    s = rng.choice({'M': ['masculino', 'sexo masculino', 'masc.', 'homem'],
                    'F': ['feminino', 'sexo feminino', 'fem.', 'mulher']}[sexo])
    modelos = [
        f'SOLICITAÇÃO DE INTERNAÇÃO ELETIVA\nPaciente: {s}, {idade} anos.\nHD: {hd}.\nProcedimento solicitado: {proc_texto}.\nCaráter: eletivo.',
        f'Encaminho paciente ({s}), {idade} anos, com {hd}, para {proc_texto} em caráter eletivo.',
        f'Pcte {s} {idade}a. Diagnóstico: {hd}. Solicito {proc_texto}. Eletiva.',
        f'Procedimento: {proc_texto}\nDiagnóstico: {hd}\nIdade: {idade} anos | Sexo: {s}\nObs.: paciente orientado(a), exames pré-operatórios em anexo.',
    ]
    return rng.choice(modelos)


def gerar_caso(i, rng):
    todos_cids = sigtap.cids()
    erro = rng.choice(ERROS) if rng.random() < 0.5 else None
    elegiveis = [c for c in PROCEDIMENTOS if com_limite_de_idade(c)] if erro == 'idade_fora_da_faixa' else list(PROCEDIMENTOS)
    codigo = rng.choice(elegiveis)
    textos, (idade_min, idade_max) = PROCEDIMENTOS[codigo]
    proc = sigtap.procedimentos()[codigo]

    cid = rng.choice(sorted(c for c in proc.cids_compativeis if c in todos_cids))
    sexo = SEXO_FIXO.get(codigo, rng.choice(['M', 'F']))
    idade = rng.randint(idade_min, idade_max)
    hd = None

    if erro == 'cid_incompativel':
        outro = rng.choice([c for c in PROCEDIMENTOS if c != codigo])
        cid = rng.choice(sorted(c for c in sigtap.procedimentos()[outro].cids_compativeis
                                if c in todos_cids and c not in proc.cids_compativeis))
    elif erro == 'cid_ausente':
        # Metade das vezes o diagnóstico vem por extenso, sem o código CID
        hd = todos_cids[cid].lower() if rng.random() < 0.5 else rng.choice(['em investigação', 'a esclarecer', 'conforme exames em anexo'])
        cid = None
    elif erro == 'idade_fora_da_faixa':
        if proc.idade_min_meses >= 24:
            idade = max(1, proc.idade_min_meses // 12 - rng.randint(1, 3))
        else:
            idade = proc.idade_max_meses // 12 + rng.randint(2, 6)

    if hd is None:
        hd = f'{todos_cids[cid].lower()} (CID {cid_formatado(cid, rng)})'
    gabarito = sigtap.validar(codigo, cid, idade)
    return {
        'id': f'caso_{i:03d}', 'texto': redigir(sexo, idade, hd, rng.choice(textos), rng), 'erro_plantado': erro,
        'esperado': {'procedimento': codigo, 'cid': cid, 'idade': idade,
                     'pendencias': sorted(gabarito.pendencias), 'apto': gabarito.apto},
    }


def main(n):
    rng = random.Random(SEED)
    casos = [gerar_caso(i, rng) for i in range(1, n + 1)]
    SAIDA.write_text('\n'.join(json.dumps(c, ensure_ascii=False) for c in casos) + '\n', encoding='utf-8')
    aptos = sum(c['esperado']['apto'] for c in casos)
    print(f'{n} casos -> {SAIDA.name} ({aptos} aptos, {n - aptos} com pendência)')
    print('Pendências no gabarito:', dict(Counter(p for c in casos for p in c['esperado']['pendencias'])))
    print('Procedimentos distintos:', len({c['esperado']['procedimento'] for c in casos}))


if __name__ == '__main__':
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 120)
