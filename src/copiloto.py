# Copiloto de pedido cirúrgico: lê um pedido em texto livre, identifica o
# procedimento SIGTAP e checa o pedido contra as regras oficiais.
#
#   1. O LLM extrai os campos e reescreve o procedimento na nomenclatura SIGTAP
#   2. Uma busca por similaridade traz os procedimentos candidatos da tabela
#   3. O LLM escolhe o código entre os candidatos
#   4. O motor de regras (sigtap.validar) decide se o pedido está apto
#
# Uso:
#   python src/copiloto.py "Pcte fem 45a. HD: colelitíase (K80.2). Solicito CCL VLP."

import os
import sys
import time
from dataclasses import dataclass, field
from typing import Literal

import openai
from dotenv import load_dotenv
from google import genai
from google.genai import errors, types
from pydantic import BaseModel, Field

import sigtap

load_dotenv()  # lê GEMINI_API_KEY e OPENROUTER_API_KEY do arquivo .env

# Nome sem barra vai direto para a API do Gemini (ex.: gemini-3.5-flash-lite).
# Nome com barra vai pelo OpenRouter (ex.: anthropic/claude-haiku-5.5).
MODELO_PADRAO = 'gemini-3.5-flash'
OPENROUTER_URL = 'https://openrouter.ai/api/v1'
# O plano gratuito limita requisições por minuto (429), tem picos de demanda (503)
# e devolve 403 intermitentes no mesmo pedido que passa na tentativa seguinte.
HTTP = types.HttpOptions(timeout=60_000, retry_options=types.HttpRetryOptions(
    attempts=8, initial_delay=2.0, max_delay=60.0, http_status_codes=[403, 429, 500, 503]))
# US$ por milhão de tokens (entrada, saída) no plano pago; tokens de raciocínio são
# cobrados como saída. O flash-lite vem da tabela oficial do Google; o 3.5 Flash não
# está na tabela oficial e o valor vem de agregadores de preço (out/2026). Os modelos
# com barra usam o preço de tabela do OpenRouter (out/2026).
PRECOS = {
    'gemini-3.5-flash-lite': (0.30, 2.50),
    'gemini-3.5-flash': (1.50, 9.00),
    'google/gemini-3.5-flash': (1.50, 9.00),
    'anthropic/claude-haiku-5.5': (0.10, 0.50),
    'anthropic/claude-sonnet-5.5': (2.00, 10.00),
}

PROMPT_EXTRACAO = """Você recebe um pedido de cirurgia eletiva escrito por um médico, em texto livre.
Extraia os campos abaixo exatamente como estão no pedido, sem inventar informação.

- cid: só o código CID-10 se ele estiver escrito no pedido (ex.: "K80.2" -> "K802"). Se o pedido traz apenas o diagnóstico por extenso, retorne null: não deduza o código.
- procedimento_sigtap: reescreva o procedimento solicitado na nomenclatura da tabela SIGTAP do SUS, por extenso e sem abreviações (ex.: "RTU de próstata" -> "ressecção endoscópica de próstata"; "apendicectomia VLP" -> "apendicectomia videolaparoscópica"). Mantenha lateralidade (unilateral/bilateral) e via de acesso (videolaparoscópica, convencional) quando o pedido informar.
- nomes_alternativos: até 3 outras formas como o mesmo procedimento pode estar nomeado na SIGTAP, que costuma usar o nome técnico da técnica (ex.: "correção de hérnia epigástrica" -> "hernioplastia epigástrica"; "retirada da tireoide" -> "tireoidectomia total"; "tonsilectomia" -> "amigdalectomia")."""

PROMPT_ESCOLHA = """Escolha, na lista de procedimentos da tabela SIGTAP abaixo, o código que corresponde ao procedimento solicitado no pedido.
Atenção a lateralidade (unilateral x bilateral), via de acesso (videolaparoscópica x convencional) e procedimentos combinados.
Responda com um código da lista. Se nenhum corresponder, responda null.

Pedido:
{pedido}

Procedimento entendido: {procedimento}

Candidatos:
{candidatos}"""


class Extracao(BaseModel):
    procedimento_texto: str = Field(description='Procedimento como escrito no pedido')
    procedimento_sigtap: str = Field(description='Procedimento na nomenclatura SIGTAP, por extenso')
    nomes_alternativos: list[str] = Field(description='Até 3 outros nomes do procedimento no padrão SIGTAP')
    cid: str | None = Field(description='Código CID-10 escrito no pedido, sem ponto, ou null')
    diagnostico_texto: str | None
    idade_anos: int | None
    sexo: Literal['M', 'F'] | None


class Escolha(BaseModel):
    codigo: str | None = Field(description='Código SIGTAP de 10 dígitos escolhido da lista, ou null')
    justificativa: str


@dataclass
class Resultado:
    extracao: Extracao | None
    codigo: str | None
    pendencias: list
    candidatos: list = field(default_factory=list)
    tokens_entrada: int = 0
    tokens_saida: int = 0
    segundos: float = 0.0
    erro: str | None = None

    @property
    def apto(self):
        return self.erro is None and not self.pendencias


class Copiloto:
    def __init__(self, modelo=MODELO_PADRAO, nomes_alternativos=True):
        self.modelo = modelo
        self.nomes_alternativos = nomes_alternativos
        self.openrouter = '/' in modelo
        if self.openrouter:
            self.client = openai.OpenAI(base_url=OPENROUTER_URL, api_key=os.environ['OPENROUTER_API_KEY'],
                                        timeout=60, max_retries=6)
        else:
            self.client = genai.Client(http_options=HTTP)

    def _perguntar(self, prompt, formato, resultado):
        if self.openrouter:
            return self._perguntar_openrouter(prompt, formato, resultado)
        resposta = self.client.models.generate_content(
            model=self.modelo,
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type='application/json', response_schema=formato),
        )
        uso = resposta.usage_metadata
        if uso:
            resultado.tokens_entrada += uso.prompt_token_count or 0
            resultado.tokens_saida += (uso.candidates_token_count or 0) + (uso.thoughts_token_count or 0)
        if resposta.parsed is None:
            motivo = resposta.candidates[0].finish_reason if resposta.candidates else resposta.prompt_feedback
            raise RuntimeError(f'resposta sem saída estruturada ({motivo})')
        return resposta.parsed

    def _perguntar_openrouter(self, prompt, formato, resultado):
        resposta = self.client.chat.completions.parse(
            model=self.modelo,
            messages=[{'role': 'user', 'content': prompt}],
            response_format=formato,
        )
        if resposta.usage:
            # completion_tokens já inclui os tokens de raciocínio
            resultado.tokens_entrada += resposta.usage.prompt_tokens or 0
            resultado.tokens_saida += resposta.usage.completion_tokens or 0
        parsed = resposta.choices[0].message.parsed if resposta.choices else None
        if parsed is None:
            motivo = resposta.choices[0].finish_reason if resposta.choices else 'sem resposta'
            raise RuntimeError(f'resposta sem saída estruturada ({motivo})')
        return parsed

    def analisar(self, pedido):
        resultado = Resultado(extracao=None, codigo=None, pendencias=[])
        inicio = time.perf_counter()
        try:
            ext = self._perguntar(f'{PROMPT_EXTRACAO}\n\nPedido:\n{pedido}', Extracao, resultado)
            resultado.extracao = ext

            vistos = {}
            buscas = [(ext.procedimento_sigtap, 10), (ext.procedimento_texto, 5)]
            if self.nomes_alternativos:
                buscas += [(nome, 5) for nome in ext.nomes_alternativos[:3]]
            for texto, n in buscas:
                for p in sigtap.candidatos(texto, n):
                    vistos.setdefault(p.codigo, p)
            resultado.candidatos = list(vistos)
            lista = '\n'.join(f'{p.codigo} - {p.nome}' for p in vistos.values())

            escolha = self._perguntar(
                PROMPT_ESCOLHA.format(pedido=pedido, procedimento=ext.procedimento_sigtap, candidatos=lista),
                Escolha, resultado)
            # Um código fora da lista de candidatos não é aceito
            resultado.codigo = escolha.codigo if escolha.codigo in vistos else None
            resultado.pendencias = sigtap.validar(resultado.codigo, ext.cid, ext.idade_anos).pendencias
        except (errors.APIError, openai.APIError, RuntimeError, ValueError) as e:
            resultado.erro = f'{type(e).__name__}: {e}'
        resultado.segundos = time.perf_counter() - inicio
        return resultado

    def custo(self, resultado):
        if self.modelo not in PRECOS:
            return 0.0
        entrada, saida = PRECOS[self.modelo]
        return (resultado.tokens_entrada * entrada + resultado.tokens_saida * saida) / 1_000_000


if __name__ == '__main__':
    if len(sys.argv) < 2:
        sys.exit('Uso: python src/copiloto.py "texto do pedido" [modelo]')
    copiloto = Copiloto(sys.argv[2] if len(sys.argv) > 2 else MODELO_PADRAO)
    r = copiloto.analisar(sys.argv[1])
    if r.erro:
        sys.exit(r.erro)
    proc = sigtap.procedimentos().get(r.codigo or '')
    print(f'Procedimento: {r.codigo} - {proc.nome if proc else "não identificado"}')
    print(f'CID: {r.extracao.cid or "não informado"} | Idade: {r.extracao.idade_anos} | Sexo: {r.extracao.sexo}')
    print('Situação: APTO' if r.apto else f'Situação: PENDENTE -> {", ".join(r.pendencias)}')
    print(f'Custo: US$ {copiloto.custo(r):.4f} | {r.segundos:.1f} s')
