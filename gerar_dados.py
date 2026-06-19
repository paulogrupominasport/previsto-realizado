# -*- coding: utf-8 -*-
"""
Grupo Minas Port - Gerador de dados do dashboard de Agendamentos (Previsto x Realizado).

Lê o PDF de agendamentos, extrai SOMENTE a primeira tabela
(ignora a seção "Realizados na Data, porém previstos em outra data",
detectada dinamicamente pela posição do texto, em qualquer página),
e gera:
  - dados.json      -> retrato atual (substituído a cada execução)
  - historico.json  -> uma entrada por dia (a do dia é sobrescrita a cada hora)

Uso:
  python gerar_dados.py CAMINHO_DO_PDF
  (se omitido, procura por 'Generico.pdf' na pasta atual)
"""
import sys, os, re, json, unicodedata
from datetime import datetime
import pdfplumber

MARCADOR_SECAO2 = "Realizados na Data"   # tudo a partir daqui é ignorado
ARQ_DADOS = "dados.json"
ARQ_HISTORICO = "historico.json"

ROTULOS_PULAR = {
    "*total do lote", "*total operação", "*total operacao",
    "total geral", "total carregamento", "total descarga",
}


# ---------- helpers ----------
def limpa(s):
    if s is None:
        return ""
    return re.sub(r"\s+", " ", s.replace("\n", " ")).strip()


def num(s):
    s = limpa(s)
    if not s:
        return 0.0
    try:
        return float(s.replace(".", "").replace(",", "."))
    except ValueError:
        return 0.0


def inteiro(s):
    s = limpa(s)
    return int(s) if s.lstrip("-").isdigit() else 0


def sem_acento(s):
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().upper()


def normaliza_produto(seg):
    """Consolida o nome do produto a partir do 2º segmento do lote."""
    s = sem_acento(seg)
    if "TERMICO" in s:
        return "TÉRMICO"
    if "PETCOKE" in s:
        return "PETCOKE"
    if "GESSO" in s:
        return "GESSO"
    if "COQUE" in s:
        return "COQUE"
    if "CARVAO" in s:
        return "CARVÃO"
    if "PETROLEO" in s:
        return "PETRÓLEO"
    if "BLEND" in s:
        return "BLEND"
    if "GUSA" in s:
        return "GUSA"
    return seg.strip().title() if seg.strip() else "OUTROS"


def cliente_produto(lote):
    """No padrão do lote, o 1º segmento é o cliente e o 2º é o produto."""
    partes = [p.strip() for p in re.split(r"\s*-\s*", lote) if p.strip()]
    cliente = partes[0] if partes else lote
    produto = normaliza_produto(partes[1]) if len(partes) > 1 else "OUTROS"
    return cliente, produto


def parse_transportadora(cel):
    """'WJ TRANSPORTES (SC) (08533312000334 )' -> (nome, cnpj)."""
    cel = limpa(cel)
    cnpj = ""
    m = re.search(r"\((\d[\d\s]*)\)\s*$", cel)
    if m:
        cnpj = re.sub(r"\s+", "", m.group(1))
        cel = cel[: m.start()].strip()
    return cel, cnpj


# ---------- parsing do PDF ----------
def eh_linha_total(cells):
    """True se a linha é subtotal/total (*Total do Lote, *Total Operação,
    TOTAL GERAL/CARREGAMENTO/DESCARGA) — em qualquer das 3 primeiras colunas."""
    txt = sem_acento(" ".join(cells[:3]))
    return ("*TOTAL" in txt or "TOTAL GERAL" in txt
            or "TOTAL CARREGAMENTO" in txt or "TOTAL DESCARGA" in txt)


def extrair_lotes(caminho_pdf):
    """
    Estado (lote em construção) PERSISTE entre páginas: assim um lote cujas
    transportadoras e/ou subtotal caem em páginas diferentes é montado certo.
    O total de cada lote é a SOMA das transportadoras (não depende da linha
    '*Total do Lote', que serve só para fechar o lote).
    """
    lotes = []
    data_relatorio = ""
    gerado_em = ""        # horário em que o PDF foi gerado (carimbo do relatório)
    lote_atual = ""
    atual = {"ref": None}  # usa dict como "ponteiro" para fechar de dentro

    def fechar_lote():
        L = atual["ref"]
        if L and L["transportadoras"]:
            t = L["transportadoras"]
            L["prog"] = sum(x["prog"] for x in t)
            L["carr"] = sum(x["carr"] for x in t)
            L["falta"] = sum(x["falta"] for x in t)
            L["tn_prev"] = round(sum(x["tn_prev"] for x in t), 4)
            L["tn_carr"] = round(sum(x["tn_carr"] for x in t), 4)
            lotes.append(L)
        atual["ref"] = None

    with pdfplumber.open(caminho_pdf) as pdf:
        # horário de geração: metadados CreationDate (D:AAAAMMDDHHMMSS-03'00')
        cd = pdf.metadata.get("CreationDate", "") or ""
        mc = re.search(r"D:(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})", cd)
        if mc:
            y, mo, d, h, mi, s = mc.groups()
            gerado_em = f"{y}-{mo}-{d}T{h}:{mi}:{s}"

        secao2_iniciada = False
        for page in pdf.pages:
            if secao2_iniciada:
                break

            texto = page.extract_text() or ""
            if not data_relatorio:
                md = re.search(r"\b(\d{2}/\d{2}/\d{4})\b", texto)
                if md:
                    data_relatorio = md.group(1)

            # fallback do horário: hora HH:MM:SS do rodapé + data do relatório
            if not gerado_em and data_relatorio:
                mh = re.search(r"\b(\d{1,2}:\d{2}:\d{2})\b", texto)
                if mh:
                    try:
                        dt = datetime.strptime(data_relatorio + " " + mh.group(1),
                                               "%d/%m/%Y %H:%M:%S")
                        gerado_em = dt.strftime("%Y-%m-%dT%H:%M:%S")
                    except ValueError:
                        pass

            # posição vertical do marcador da seção 2 (se existir nesta página)
            corte_y = None
            for w in page.extract_words():
                if w["text"].startswith("Realizados"):
                    corte_y = w["top"]
                    break

            for tb in page.find_tables():
                for row in tb.rows:
                    # ignora linhas que estão na seção 2 (abaixo do marcador)
                    if corte_y is not None and row.bbox[1] >= corte_y:
                        continue
                    cells = []
                    for c in row.cells:
                        cells.append(limpa(page.crop(c).extract_text()) if c else "")
                    if len(cells) < 8:
                        continue
                    tipo, lote, transp = cells[0], cells[1], cells[2]
                    if tipo == "Tipo Operação":  # cabeçalho repetido
                        continue

                    # qualquer linha de total fecha o lote pendente e é ignorada
                    if eh_linha_total(cells):
                        fechar_lote()
                        continue

                    # início de um novo lote (fecha o anterior, se houver)
                    if lote and lote != lote_atual:
                        fechar_lote()
                        lote_atual = lote
                        cli, prod = cliente_produto(lote)
                        atual["ref"] = {
                            "lote": lote, "cliente": cli, "produto": prod,
                            "prog": 0, "carr": 0, "falta": 0,
                            "tn_prev": 0.0, "tn_carr": 0.0, "transportadoras": [],
                        }

                    # linha de transportadora (acumula no lote atual)
                    if atual["ref"] and transp:
                        nome, cnpj = parse_transportadora(transp)
                        if nome:
                            atual["ref"]["transportadoras"].append({
                                "nome": nome, "cnpj": cnpj,
                                "prog": inteiro(cells[3]), "carr": inteiro(cells[4]),
                                "falta": inteiro(cells[5]),
                                "tn_prev": num(cells[6]), "tn_carr": num(cells[7]),
                            })

            if corte_y is not None:
                secao2_iniciada = True

        fechar_lote()  # fecha o último lote, se sobrou algum aberto

    return lotes, data_relatorio, gerado_em


# ---------- agregações ----------
def agrega(lotes, chave):
    acc = {}
    for L in lotes:
        k = L[chave]
        a = acc.setdefault(k, {chave: k, "prog": 0, "carr": 0, "falta": 0,
                               "tn_prev": 0.0, "tn_carr": 0.0})
        for campo in ("prog", "carr", "falta", "tn_prev", "tn_carr"):
            a[campo] += L[campo]
    saida = sorted(acc.values(), key=lambda x: x["tn_prev"], reverse=True)
    for a in saida:
        a["tn_prev"] = round(a["tn_prev"], 2)
        a["tn_carr"] = round(a["tn_carr"], 2)
    return saida


def main():
    caminho = sys.argv[1] if len(sys.argv) > 1 else "Generico.pdf"
    if not os.path.exists(caminho):
        print(f"ERRO: PDF não encontrado em '{caminho}'")
        sys.exit(1)

    lotes, data_rel, gerado_em = extrair_lotes(caminho)
    if not lotes:
        print("AVISO: nenhum lote extraído. PDF mudou de layout? Abortando para não apagar dados bons.")
        sys.exit(1)

    totais = {
        "prog": sum(l["prog"] for l in lotes),
        "carr": sum(l["carr"] for l in lotes),
        "falta": sum(l["falta"] for l in lotes),
        "tn_prev": round(sum(l["tn_prev"] for l in lotes), 2),
        "tn_carr": round(sum(l["tn_carr"] for l in lotes), 2),
    }
    por_cliente = agrega(lotes, "cliente")
    por_produto = agrega(lotes, "produto")

    agora = datetime.now()
    dados = {
        "data_relatorio": data_rel,
        "gerado_em": gerado_em,                               # horário do PDF (carimbo do relatório)
        "atualizado_em": agora.strftime("%Y-%m-%dT%H:%M:%S"), # horário em que a Action processou
        "totais": totais,
        "por_cliente": por_cliente,
        "por_produto": por_produto,
        "lotes": sorted(lotes, key=lambda x: x["tn_prev"], reverse=True),
    }
    with open(ARQ_DADOS, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)

    # ----- histórico: chave por data do relatório (sobrescreve a do dia) -----
    if data_rel:
        try:
            data_iso = datetime.strptime(data_rel, "%d/%m/%Y").strftime("%Y-%m-%d")
        except ValueError:
            data_iso = agora.strftime("%Y-%m-%d")

        historico = {}
        if os.path.exists(ARQ_HISTORICO):
            try:
                with open(ARQ_HISTORICO, encoding="utf-8") as f:
                    historico = json.load(f)
            except (json.JSONDecodeError, OSError):
                historico = {}

        historico[data_iso] = {
            "gerado_em": gerado_em,
            "atualizado_em": dados["atualizado_em"],
            "totais": totais,
            "por_cliente": {c["cliente"]: {"prog": c["prog"], "carr": c["carr"],
                                           "tn_prev": c["tn_prev"], "tn_carr": c["tn_carr"]}
                            for c in por_cliente},
            "por_produto": {p["produto"]: {"prog": p["prog"], "carr": p["carr"],
                                           "tn_prev": p["tn_prev"], "tn_carr": p["tn_carr"]}
                            for p in por_produto},
        }
        # mantém só os últimos 60 dias
        for k in sorted(historico.keys())[:-60]:
            del historico[k]

        with open(ARQ_HISTORICO, "w", encoding="utf-8") as f:
            json.dump(historico, f, ensure_ascii=False, indent=2)

    print(f"OK: {len(lotes)} lotes | data {data_rel} | "
          f"prog {totais['prog']} carr {totais['carr']} falta {totais['falta']} | "
          f"prev {totais['tn_prev']} carr {totais['tn_carr']}")


if __name__ == "__main__":
    main()
