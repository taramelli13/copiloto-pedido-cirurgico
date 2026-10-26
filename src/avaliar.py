# Avalia o copiloto nos casos de data/casos.jsonl, para um ou mais modelos.
#
# Uso:
#   python src/avaliar.py --modelos anthropic/claude-haiku-5.5 gemini-3.5-flash-lite
#   python src/avaliar.py --modelos anthropic/claude-haiku-5.5 --sem-nomes-alternativos
#
# Cada caso é gravado em resultados/{modelo}.jsonl assim que termina. Rodar de
# novo retoma de onde parou: casos já avaliados são pulados e casos que deram
# erro de API são refeitos. Ao final, o resumo de cada modelo vai para
# resultados/resumo.json.

import argparse
import json
import statistics
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import sigtap
from copiloto import PRECOS, Copiloto

RAIZ = Path(__file__).resolve().parent.parent
CASOS = RAIZ / 'data' / 'casos.jsonl'
SAIDA = RAIZ / 'resultados'
# Cota diária do Gemini ou créditos do OpenRouter esgotados: não adianta tentar de novo agora
COTA_ESGOTADA = ('RESOURCE_EXHAUSTED', 'free_tier_requests', 'Error code: 402')


def ler_jsonl(caminho):
    if not caminho.exists():
        return []
    return [json.loads(l) for l in caminho.read_text(encoding='utf-8').splitlines() if l.strip()]


def linha(caso, r):
    esp = caso['esperado']
    ext = r.extracao
    return {
        'id': caso['id'], 'erro_plantado': caso['erro_plantado'], 'erro_api': r.erro,
        'codigo': r.codigo, 'codigo_esperado': esp['procedimento'],
        'esperado_nos_candidatos': esp['procedimento'] in r.candidatos,
        'procedimento_entendido': ext.procedimento_sigtap if ext else None,
        'cid': sigtap.normalizar_cid(ext.cid) if ext else None, 'cid_esperado': esp['cid'],
        'idade': ext.idade_anos if ext else None, 'idade_esperada': esp['idade'],
        'pendencias': sorted(r.pendencias), 'pendencias_esperadas': esp['pendencias'],
        'apto': r.apto, 'apto_esperado': esp['apto'],
        'tokens_entrada': r.tokens_entrada, 'tokens_saida': r.tokens_saida, 'segundos': round(r.segundos, 2),
    }


def avaliar_modelo(modelo, casos, paralelo, nomes_alternativos=True):
    rotulo = modelo if nomes_alternativos else f'{modelo} (sem nomes alternativos)'
    arquivo = SAIDA / f'{modelo.replace("/", "__")}{"" if nomes_alternativos else "__sem-alternativos"}.jsonl'
    feitas = {l['id']: l for l in ler_jsonl(arquivo) if not l['erro_api']}
    pendentes = [c for c in casos if c['id'] not in feitas]
    print(f'{rotulo}: {len(feitas)} casos já avaliados, {len(pendentes)} a avaliar')

    copiloto = Copiloto(modelo, nomes_alternativos)
    parar = threading.Event()
    trava = threading.Lock()

    def rodar(caso):
        return None if parar.is_set() else copiloto.analisar(caso['texto'])

    with ThreadPoolExecutor(paralelo) as pool:
        futuros = {pool.submit(rodar, c): c for c in pendentes}
        for i, futuro in enumerate(as_completed(futuros), 1):
            r = futuro.result()
            if r is None:
                continue
            if r.erro and any(m in r.erro for m in COTA_ESGOTADA):
                if not parar.is_set():
                    print(f'{modelo}: cota diária esgotada, parando. Rode de novo amanhã para continuar.')
                parar.set()
                continue
            l = linha(futuros[futuro], r)
            with trava:
                with arquivo.open('a', encoding='utf-8') as f:
                    f.write(json.dumps(l, ensure_ascii=False) + '\n')
                if not l['erro_api']:
                    feitas[l['id']] = l
            print(f'  [{i}/{len(pendentes)}] {l["id"]} {"ERRO " + l["erro_api"][:80] if l["erro_api"] else "ok"}', flush=True)

    # Regrava o arquivo só com a última avaliação válida de cada caso, na ordem dos casos
    linhas = [feitas[c['id']] for c in casos if c['id'] in feitas]
    arquivo.write_text(''.join(json.dumps(l, ensure_ascii=False) + '\n' for l in linhas), encoding='utf-8')
    return {**resumir(modelo, linhas, len(casos)), 'modelo': rotulo}


def taxa(itens, cond):
    itens = list(itens)
    return round(sum(map(cond, itens)) / len(itens), 4) if itens else None


def custo(modelo, l):
    if modelo not in PRECOS:
        return 0.0
    entrada, saida = PRECOS[modelo]
    return (l['tokens_entrada'] * entrada + l['tokens_saida'] * saida) / 1_000_000


def resumir(modelo, linhas, total_casos):
    if not linhas:
        return {'modelo': modelo, 'casos_avaliados': 0, 'casos_total': total_casos}
    com_pendencia = [l for l in linhas if not l['apto_esperado']]
    sem_pendencia = [l for l in linhas if l['apto_esperado']]
    custo_medio = statistics.mean(custo(modelo, l) for l in linhas)
    return {
        'modelo': modelo,
        'casos_avaliados': len(linhas),
        'casos_total': total_casos,
        # Busca: o código certo chegou ao LLM? Escolha: dado que chegou, ele escolheu certo?
        'busca_trouxe_codigo_certo': taxa(linhas, lambda l: l['esperado_nos_candidatos']),
        'acerto_escolha': taxa([l for l in linhas if l['esperado_nos_candidatos']],
                               lambda l: l['codigo'] == l['codigo_esperado']),
        'acerto_procedimento': taxa(linhas, lambda l: l['codigo'] == l['codigo_esperado']),
        'acerto_cid': taxa(linhas, lambda l: l['cid'] == l['cid_esperado']),
        'acerto_idade': taxa(linhas, lambda l: l['idade'] == l['idade_esperada']),
        'acerto_decisao': taxa(linhas, lambda l: l['apto'] == l['apto_esperado']),
        'acerto_pendencias_exatas': taxa(linhas, lambda l: l['pendencias'] == l['pendencias_esperadas']),
        'pedidos_com_problema_detectados': taxa(com_pendencia, lambda l: not l['apto']),
        'alarmes_falsos': taxa(sem_pendencia, lambda l: not l['apto']),
        'tokens_medios': round(statistics.mean(l['tokens_entrada'] + l['tokens_saida'] for l in linhas)),
        'custo_por_mil_pedidos_usd': round(custo_medio * 1000, 2),
        'segundos_mediana': round(statistics.median(l['segundos'] for l in linhas), 2),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--modelos', nargs='+', required=True)
    parser.add_argument('--n', type=int, default=None, help='Avaliar só os N primeiros casos')
    parser.add_argument('--paralelo', type=int, default=2)
    parser.add_argument('--sem-nomes-alternativos', action='store_true',
                        help='Busca só pelo nome do procedimento, sem os nomes alternativos (versão 1)')
    args = parser.parse_args()

    SAIDA.mkdir(exist_ok=True)
    casos = ler_jsonl(CASOS)[:args.n]
    anterior = SAIDA / 'resumo.json'
    resumos = {r['modelo']: r for r in json.loads(anterior.read_text(encoding='utf-8'))} if anterior.exists() else {}
    for modelo in args.modelos:
        resumo = avaliar_modelo(modelo, casos, args.paralelo, not args.sem_nomes_alternativos)
        resumos[resumo['modelo']] = resumo
        print(json.dumps(resumo, indent=2, ensure_ascii=False))
    (SAIDA / 'resumo.json').write_text(json.dumps(list(resumos.values()), indent=2, ensure_ascii=False), encoding='utf-8')


if __name__ == '__main__':
    main()
