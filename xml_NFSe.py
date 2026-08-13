import re
import sys
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

APP_NAME = "Normalizador XML NFS-e -> GerarNfseEnvio"
PASTA_SAIDA = "XML_Corrigidos"
NS_NACIONAL = "http://www.sped.fazenda.gov.br/nfse"


def ler_xml(caminho: Path) -> str:
    dados = caminho.read_bytes()

    if dados.startswith(b"\xef\xbb\xbf"):
        return dados.decode("utf-8-sig")
    if dados.startswith(b"\xff\xfe"):
        return dados.decode("utf-16-le")
    if dados.startswith(b"\xfe\xff"):
        return dados.decode("utf-16-be")

    cabecalho = dados[:300].decode("ascii", errors="ignore")
    m = re.search(r'encoding\s*=\s*["\']([^"\']+)["\']', cabecalho, re.I)
    if m:
        try:
            return dados.decode(m.group(1))
        except (LookupError, UnicodeDecodeError):
            pass

    try:
        return dados.decode("utf-8")
    except UnicodeDecodeError:
        return dados.decode("latin-1")


def remover_declaracao_xml(xml: str) -> str:
    return re.sub(r'^\s*<\?xml[^>]*\?>\s*', '', xml, flags=re.I)


def local_name(tag: str) -> str:
    return tag.split('}', 1)[-1] if '}' in tag else tag


def child(parent, nome: str):
    if parent is None:
        return None
    for el in list(parent):
        if local_name(el.tag) == nome:
            return el
    return None


def text(parent, caminho: list[str], default: str = "") -> str:
    atual = parent
    for nome in caminho:
        atual = child(atual, nome)
        if atual is None:
            return default
    return (atual.text or "").strip()


def tag(nome: str, valor) -> str:
    if valor is None or str(valor) == "":
        return ""
    return f"<{nome}>{escape(str(valor))}</{nome}>"


def formatar_data_hora(valor: str) -> str:
    valor = (valor or "").strip()
    if not valor:
        return ""
    if re.search(r'(Z|[+-]\d\d:\d\d)$', valor):
        return valor
    if 'T' in valor:
        return valor + "-03:00"
    return valor + "T00:00:00-03:00"


def detectar_padrao(xml: str) -> str:
    # Ordem importante: GerarNfseEnvio também contém DPS.
    if re.search(r'<GerarNfseEnvio\b', xml):
        return "GERAR_NACIONAL"
    if re.search(r'<NFSe\b', xml):
        return "NFSE_NACIONAL"
    if re.search(r'<Nfse\b', xml):
        return "ABRASF_204"
    raise ValueError("Formato não reconhecido. Não encontrei GerarNfseEnvio, NFSe ou Nfse.")


def numero_dps_do_bloco(dps: str) -> str:
    m = re.search(r'<nDPS>\s*(.*?)\s*</nDPS>', dps, re.S)
    return m.group(1).strip() if m else "SEM_NUMERO"


def extrair_bloco_gerar_existente(xml: str) -> tuple[str, str]:
    """Se já está no formato correto, apenas isola o GerarNfseEnvio."""
    m = re.search(r'(<GerarNfseEnvio\b[^>]*>.*?</GerarNfseEnvio>)', xml, re.S)
    if not m:
        raise ValueError("Não foi possível extrair <GerarNfseEnvio>.")
    bloco = m.group(1)
    return bloco, numero_dps_do_bloco(bloco)


def extrair_dps_de_nfse_nacional(xml: str) -> tuple[str, str]:
    """
    Recebe XML Nacional autorizado/consultado e reaproveita a DPS original.
    Não reconstrói o conteúdo da DPS, preservando inclusive Signature quando existir.
    """
    m = re.search(r'(<DPS\b[^>]*>.*?</DPS>)', xml, re.S)
    if not m:
        raise ValueError("A NFS-e Nacional não contém uma <DPS> para reaproveitar.")

    dps = m.group(1)
    numero = numero_dps_do_bloco(dps)
    bloco = f'<GerarNfseEnvio xmlns="{NS_NACIONAL}">\n{dps}\n</GerarNfseEnvio>'
    return bloco, numero


def mapear_item_lista_para_ctribnac(item: str) -> str:
    """
    Ex.: 14.01 -> 140101, conforme os XMLs Nacionais do mesmo serviço fornecidos.
    """
    digitos = re.sub(r'\D', '', item or '')
    if len(digitos) == 4:
        return digitos + "01"
    if len(digitos) == 6:
        return digitos
    return ""


def gerar_id_dps(c_mun: str, cnpj: str, cpf: str, serie: str, n_dps: str) -> str:
    """
    Identificador no formato usado pelo Sistema Nacional:
    Município(7) + Tipo inscrição(1) + Inscrição federal(14) + Série(5) + Número DPS(15).
    Para CPF, completa a inscrição federal com zeros à esquerda até 14 posições.
    """
    c_mun = re.sub(r'\D', '', c_mun or '').zfill(7)[-7:]
    serie = re.sub(r'\D', '', serie or '1').zfill(5)[-5:]
    n_dps = re.sub(r'\D', '', n_dps or '0').zfill(15)[-15:]

    if cnpj:
        tipo = '2'
        inscr = re.sub(r'\D', '', cnpj).zfill(14)[-14:]
    else:
        tipo = '1'
        inscr = re.sub(r'\D', '', cpf or '').zfill(14)[-14:]

    return f"DPS{c_mun}{tipo}{inscr}{serie}{n_dps}"


def mapear_regime_simples(optante_abrasf: str) -> tuple[str, str]:
    """
    ABRASF: 1=Sim, 2=Não.
    Nos XMLs Nacionais fornecidos:
      - não optante -> opSimpNac=1
      - optante ME/EPP -> opSimpNac=3 e regApTribSN=1
    Retorna (opSimpNac, bloco opcional regApTribSN).
    """
    if str(optante_abrasf).strip() == '1':
        return '3', '<regApTribSN>1</regApTribSN>'
    return '1', ''


def converter_abrasf_para_gerar(xml: str) -> tuple[str, str]:
    """
    Converte o conteúdo útil de uma NFS-e ABRASF 2.04 para a estrutura:

      GerarNfseEnvio
        DPS versao=1.01
          infDPS

    A assinatura ABRASF NÃO é copiada, pois ela não é válida para a nova estrutura.
    O resultado é útil para o seu fluxo de leitura/impressão/normalização.
    Se o arquivo for retransmitido, deverá ser validado e assinado novamente pelo emissor.
    """
    raiz = ET.fromstring(remover_declaracao_xml(xml))

    nfse = next((el for el in raiz.iter() if local_name(el.tag) == 'Nfse'), None)
    if nfse is None:
        raise ValueError("Elemento <Nfse> ABRASF não encontrado.")

    inf = child(nfse, 'InfNfse')
    if inf is None:
        raise ValueError("Elemento <InfNfse> não encontrado.")

    declaracao = child(inf, 'DeclaracaoPrestacaoServico')
    inf_decl = child(declaracao, 'InfDeclaracaoPrestacaoServico')
    if inf_decl is None:
        raise ValueError("Elemento <InfDeclaracaoPrestacaoServico> não encontrado.")

    # RPS / competência
    n_dps = text(inf_decl, ['Rps', 'IdentificacaoRps', 'Numero']) or text(inf, ['Numero'])
    serie = text(inf_decl, ['Rps', 'IdentificacaoRps', 'Serie'], '1')
    competencia = text(inf_decl, ['Competencia']) or text(inf_decl, ['Rps', 'DataEmissao'])
    data_emissao = formatar_data_hora(text(inf, ['DataEmissao']) or competencia)

    # Prestador
    prest = child(inf_decl, 'Prestador')
    cnpj_prest = text(prest, ['CpfCnpj', 'Cnpj'])
    cpf_prest = text(prest, ['CpfCnpj', 'Cpf'])
    im_prest = text(prest, ['InscricaoMunicipal'])

    prest_serv = child(inf, 'PrestadorServico')
    end_prest = child(prest_serv, 'Endereco')
    c_mun_emit = text(end_prest, ['CodigoMunicipio'])
    if not c_mun_emit:
        c_mun_emit = text(child(inf, 'OrgaoGerador'), ['CodigoMunicipio'])

    # Regime tributário
    optante_abrasf = text(inf_decl, ['OptanteSimplesNacional'], '2')
    op_simp_nac, reg_ap_sn = mapear_regime_simples(optante_abrasf)

    # Tomador
    tomador = child(inf_decl, 'TomadorServico')
    id_tomador = child(tomador, 'IdentificacaoTomador')
    cnpj_toma = text(id_tomador, ['CpfCnpj', 'Cnpj'])
    cpf_toma = text(id_tomador, ['CpfCnpj', 'Cpf'])
    nome_toma = text(tomador, ['RazaoSocial'])
    end_toma = child(tomador, 'Endereco')
    contato_toma = child(tomador, 'Contato')

    # Serviço
    servico = child(inf_decl, 'Servico')
    valor_serv = text(servico, ['Valores', 'ValorServicos'], '0.00')
    item_lista = text(servico, ['ItemListaServico'])
    c_trib_nac = mapear_item_lista_para_ctribnac(item_lista)
    c_trib_mun = text(servico, ['CodigoTributacaoMunicipio'])
    c_nbs = text(servico, ['CodigoNbs'])
    descricao = text(servico, ['Discriminacao'])
    c_mun_prestacao = text(servico, ['CodigoMunicipio']) or c_mun_emit

    # Retenção ISS: nos exemplos atuais do ERP o valor utilizado foi 1.
    # Mantemos a mesma convenção de compatibilidade do fluxo já existente.
    tp_ret_issqn = '1'

    id_dps = gerar_id_dps(c_mun_emit, cnpj_prest, cpf_prest, serie, n_dps)

    prest_doc = tag('CNPJ', cnpj_prest) if cnpj_prest else tag('CPF', cpf_prest)
    toma_doc = tag('CNPJ', cnpj_toma) if cnpj_toma else tag('CPF', cpf_toma)

    bloco = f'''<GerarNfseEnvio xmlns="{NS_NACIONAL}">
  <DPS versao="1.01">
    <infDPS Id="{escape(id_dps)}">
      <tpAmb>1</tpAmb>
      {tag('dhEmi', data_emissao)}
      <verAplic>CONVERSAO_ABRASF_2.04</verAplic>
      {tag('serie', serie)}
      {tag('nDPS', n_dps)}
      {tag('dCompet', competencia)}
      <tpEmit>1</tpEmit>
      {tag('cLocEmi', c_mun_emit)}
      <prest>
        {prest_doc}
        {tag('IM', im_prest)}
        <regTrib>
          <opSimpNac>{op_simp_nac}</opSimpNac>
          {reg_ap_sn}
          <regEspTrib>0</regEspTrib>
        </regTrib>
      </prest>
      <toma>
        {toma_doc}
        {tag('xNome', nome_toma)}
        <end>
          <endNac>
            {tag('cMun', text(end_toma, ['CodigoMunicipio']))}
            {tag('CEP', text(end_toma, ['Cep']))}
          </endNac>
          {tag('xLgr', text(end_toma, ['Endereco']))}
          {tag('nro', text(end_toma, ['Numero']))}
          {tag('xCpl', text(end_toma, ['Complemento']))}
          {tag('xBairro', text(end_toma, ['Bairro']))}
        </end>
        {tag('fone', text(contato_toma, ['Telefone']))}
        {tag('email', text(contato_toma, ['Email']))}
      </toma>
      <serv>
        <locPrest>
          {tag('cLocPrestacao', c_mun_prestacao)}
        </locPrest>
        <cServ>
          {tag('cTribNac', c_trib_nac)}
          {tag('cTribMun', c_trib_mun)}
          {tag('xDescServ', descricao)}
          {tag('cNBS', c_nbs)}
        </cServ>
      </serv>
      <valores>
        <vServPrest>
          {tag('vServ', valor_serv)}
        </vServPrest>
        <trib>
          <tribMun>
            <tribISSQN>1</tribISSQN>
            <tpRetISSQN>{tp_ret_issqn}</tpRetISSQN>
          </tribMun>
          <totTrib>
            <vTotTrib>
              <vTotTribFed>0.00</vTotTribFed>
              <vTotTribEst>0.00</vTotTribEst>
              <vTotTribMun>0.00</vTotTribMun>
            </vTotTrib>
          </totTrib>
        </trib>
      </valores>
      <IBSCBS>
        <finNFSe>0</finNFSe>
        <indFinal>1</indFinal>
        <cIndOp>050101</cIndOp>
        <indDest>0</indDest>
        <valores>
          <trib>
            <gIBSCBS>
              <CST>000</CST>
              <cClassTrib>000001</cClassTrib>
            </gIBSCBS>
          </trib>
        </valores>
      </IBSCBS>
    </infDPS>
  </DPS>
</GerarNfseEnvio>'''

    return bloco, n_dps


def normalizar_xml(caminho: Path) -> tuple[Path, str, str]:
    xml = ler_xml(caminho)
    padrao = detectar_padrao(xml)

    pasta = caminho.parent / PASTA_SAIDA
    pasta.mkdir(parents=True, exist_ok=True)

    if padrao == 'GERAR_NACIONAL':
        bloco, numero = extrair_bloco_gerar_existente(xml)
        modo = 'Já estava em GerarNfseEnvio / DPS 1.01'

    elif padrao == 'NFSE_NACIONAL':
        bloco, numero = extrair_dps_de_nfse_nacional(xml)
        modo = 'NFS-e Nacional → GerarNfseEnvio / DPS 1.01'

    else:
        bloco, numero = converter_abrasf_para_gerar(xml)
        modo = 'ABRASF 2.04 → GerarNfseEnvio / DPS 1.01'

    destino = pasta / f"DPS_{numero}_GerarNfseEnvio.xml"
    conteudo = '<?xml version="1.0" encoding="UTF-8"?>\n' + bloco + '\n'
    destino.write_text(conteudo, encoding='utf-8', newline='')
    return destino, numero, modo


def processar_arquivos(arquivos):
    sucessos, erros = [], []
    for nome in arquivos:
        caminho = Path(nome)
        try:
            destino, numero, modo = normalizar_xml(caminho)
            sucessos.append((caminho.name, numero, modo, destino))
        except Exception as exc:
            erros.append((caminho.name, str(exc)))
    return sucessos, erros


def selecionar_xmls():
    arquivos = filedialog.askopenfilenames(
        title='Selecione os XMLs de NFS-e',
        filetypes=[('Arquivos XML', '*.xml'), ('Todos os arquivos', '*.*')],
    )
    if not arquivos:
        return

    sucessos, erros = processar_arquivos(arquivos)
    linhas = []

    if sucessos:
        linhas.append(f'Processados com sucesso: {len(sucessos)}')
        for _, numero, modo, _ in sucessos:
            linhas.append(f'  • DPS {numero}: {modo}')
        linhas.append('')
        linhas.append(f"Saída: pasta '{PASTA_SAIDA}'.")
        linhas.append('A raiz de todos os arquivos gerados é <GerarNfseEnvio>.')

    if erros:
        if linhas:
            linhas.append('')
        linhas.append(f'Não processados: {len(erros)}')
        for nome, erro in erros:
            linhas.append(f'  • {nome}: {erro}')

    msg = '\n'.join(linhas)
    if sucessos and not erros:
        messagebox.showinfo('Concluído', msg)
    elif sucessos:
        messagebox.showwarning('Concluído com avisos', msg)
    else:
        messagebox.showerror('Erro', msg)


def criar_interface():
    root = tk.Tk()
    root.title(APP_NAME)
    root.geometry('700x410')
    root.resizable(False, False)

    frame = tk.Frame(root, padx=30, pady=24)
    frame.pack(fill='both', expand=True)

    tk.Label(
        frame,
        text='Normalizador XML NFS-e',
        font=('Segoe UI', 20, 'bold'),
    ).pack(pady=(0, 12))

    tk.Label(
        frame,
        text=(
            'Todo XML de saída será padronizado como:\n\n'
            '<GerarNfseEnvio xmlns="http://www.sped.fazenda.gov.br/nfse">\n'
            '    <DPS versao="1.01">\n'
            '        <infDPS> ...\n\n'
            '• ABRASF 2.04: converte os dados para DPS 1.01.\n'
            '• NFSe Nacional: reaproveita a DPS existente.\n'
            '• GerarNfseEnvio: mantém o formato já correto.'
        ),
        font=('Segoe UI', 10),
        justify='left',
    ).pack()

    tk.Button(
        frame,
        text='Selecionar XMLs',
        command=selecionar_xmls,
        width=28,
        height=2,
        font=('Segoe UI', 11, 'bold'),
    ).pack(pady=24)

    tk.Label(
        frame,
        text=(
            f'Arquivos gerados em: {PASTA_SAIDA}\n'
            'Os arquivos originais nunca são alterados.\n\n'
            'Observação: uma DPS reconstruída a partir de ABRASF precisa ser '\
            'validada/assinada novamente antes de qualquer retransmissão fiscal.'
        ),
        font=('Segoe UI', 9),
        justify='left',
    ).pack()

    root.mainloop()


if __name__ == '__main__':
    try:
        criar_interface()
    except Exception as exc:
        print(f'Erro fatal: {exc}', file=sys.stderr)
        raise