# Fluxo operacional OFX

Este guia descreve como usar o OFX como base de validacao bancaria dos pagamentos. O objetivo e reduzir digitacao, encontrar inconsistencias e fechar o mes com mais seguranca, sem aprovar nada automaticamente.

## Quando importar OFX

Importe o OFX quando o extrato bancario do periodo ja estiver disponivel, de preferencia no fechamento do mes ou em revisoes semanais.

Fluxo recomendado:

1. Registre pagamentos no dia a dia pelo Telegram ou pela interface web.
2. No fechamento, importe o OFX do periodo em `OFX`.
3. Revise as sugestoes criadas pelo OFX.
4. Corrija centro de custo, obra, categoria e contraparte quando necessario.
5. Aprove os pagamentos conferidos.
6. Abra o fechamento mensal e gere as planilhas quando nao houver bloqueios.

## Telegram, lancamento manual e OFX

Telegram:

- E usado para enviar comprovante, texto, imagem ou PDF no momento do pagamento.
- Pode gerar rascunhos e sugestoes de lancamento.
- Sempre exige revisao e confirmacao antes do pagamento ser aprovado.

Lancamento manual:

- E usado quando voce quer criar ou corrigir um pagamento diretamente pela web.
- E util para despesas sem comprovante, ajustes ou lancamentos que nao vieram pelo Telegram.
- Tambem deve passar por revisao/aprovacao conforme o status.

OFX:

- E a fonte bancaria usada para validar o que saiu da conta.
- Pode criar Payments sugeridos para despesas que ainda nao foram lancadas.
- Pode identificar CPF/CNPJ, nome do favorecido, valor, data, forma de pagamento e possiveis duplicidades.
- Nao deve aprovar pagamento sozinho.

## Revisar transacoes OFX

Acesse:

```text
/interno/ofx/pendentes/
```

Use os filtros de mes/ano e status para revisar:

- `Sem lançamento`: despesa bancaria sem Payment relacionado.
- `Com Payment sugerido`: transacao OFX que ja gerou sugestao.
- `Pendente de cadastro`: sugestao sem fornecedor/trabalhador confirmado.
- `Pendente de confirmação`: sugestao pronta para revisao e aprovacao.
- `Possível duplicada`: pode ja existir como lancamento.
- `Divergente`: existe divergencia de valor, data ou contraparte.
- `Ignorada por crédito`: credito/receita que nao entra como despesa.

Ao revisar uma linha, confira data, valor, favorecido, CPF/CNPJ extraido, memo resumido, status do OFX e Payment relacionado.

## Cadastrar fornecedor ou trabalhador pendente

Quando uma sugestao OFX ficar como `Pendente de cadastro`:

1. Abra a tela OFX.
2. Localize a transacao.
3. Use o botao de cadastro rapido de fornecedor ou trabalhador.
4. Confira nome e CPF/CNPJ sugeridos.
5. Salve o cadastro.
6. Volte para a revisao OFX e continue a classificacao do Payment.

Regra pratica:

- Empresa, loja, prestador PJ ou material: geralmente fornecedor.
- Pessoa fisica recebendo por servico de obra: geralmente trabalhador.
- Se houver duvida, deixe pendente e confirme antes de cadastrar.

## Ajustar centro de custo e obra em lote

Use a edicao em lote quando varias sugestoes OFX tiverem a mesma classificacao.

Exemplos ficticios:

- Varios pagamentos de material da Obra Tacima.
- Varios impostos ou tarifas que pertencem ao centro de custo Empresa.
- Varios pagamentos de mao de obra da mesma obra.

Passos:

1. Filtre a tela OFX por periodo e por `Com Payment sugerido`.
2. Selecione os pagamentos que devem receber a mesma classificacao.
3. Preencha os campos comuns, como categoria, centro de custo, obra, forma de pagamento, quem paga ou conta bancaria.
4. Aplique a edicao em lote.
5. Revise se os itens ficaram corretos antes de aprovar.

Cuidados:

- Se escolher uma obra, o centro de custo deve ficar coerente com `Obra`.
- Se escolher `Empresa`, limpe a obra apenas quando tiver certeza de que nao se trata de gasto de obra.
- Edicao em lote nao aprova pagamento.

## Aprovar em lote

A aprovacao em lote serve para pagamentos sugeridos pelo OFX que ja estao revisados.

Pode aprovar em lote quando:

- O status estiver `Pendente de confirmação`.
- Fornecedor/trabalhador estiver preenchido.
- Categoria estiver preenchida.
- Centro de custo estiver preenchido.
- Nao houver duplicidade ou divergencia.
- Os campos obrigatorios para exportacao estiverem completos.

Nao aprovar em lote quando:

- O status estiver `Pendente de cadastro`.
- A transacao estiver marcada como possivel duplicidade.
- Houver divergencia.
- Faltar categoria, centro de custo, contraparte ou outro campo obrigatorio.
- O pagamento ainda precisar de decisao humana.

## Possivel duplicidade

Uma duplicidade pode acontecer quando o pagamento ja foi lancado pelo Telegram ou manualmente e depois apareceu no OFX.

Como agir:

1. Abra a transacao marcada como `Possível duplicada`.
2. Compare data, valor e fornecedor/trabalhador.
3. Se for o mesmo pagamento, confirme ou mantenha a conciliacao correta.
4. Se nao for o mesmo pagamento, corrija a classificacao ou crie o lancamento adequado.
5. Nao aprove em lote enquanto a duplicidade nao estiver resolvida.

Regra: o mesmo gasto nao deve gerar dois Payments exportaveis.

## Divergencia

Divergencia indica que o sistema encontrou conflito entre OFX e lancamento.

Exemplos ficticios:

- OFX mostra R$ 1.250,00, mas o lancamento esta R$ 1.200,00.
- Data bancaria e data informada estao diferentes.
- Favorecido do comprovante nao bate com o favorecido do OFX.

Como agir:

1. Abra a transacao divergente.
2. Confira o comprovante, o memo do OFX e o Payment relacionado.
3. Corrija data, valor, contraparte, categoria ou centro de custo se necessario.
4. Confirme a conciliacao somente quando os dados fizerem sentido.

Divergencias nao resolvidas bloqueiam o fechamento mensal.

## Credito ou receita

Creditos e receitas do OFX nao entram no fluxo inicial de despesas.

Quando aparecer credito:

- O sistema deve marcar como ignorado por credito/receita.
- Isso nao deve criar Payment.
- Isso nao deve bloquear o fechamento mensal.
- Voce pode consultar esses itens na tela OFX usando o filtro `Ignorada por crédito`.

Exemplo ficticio:

```text
Credito recebido de cliente - R$ 5.000,00
```

Esse valor nao deve virar despesa.

## Fechar o mes

Acesse:

```text
/interno/fechamento/
```

Antes de gerar planilhas, confira o checklist:

- Rascunhos ativos.
- Lancamentos pendentes de cadastro.
- Lancamentos pendentes de confirmacao.
- Lancamentos em correcao.
- Despesas OFX sem lancamento.
- Sugestoes OFX pendentes.
- Duplicidades.
- Divergencias.
- Campos obrigatorios faltantes.
- OFX importado no periodo.
- Creditos ignorados.

Bloqueiam a geracao:

- Despesa OFX sem Payment.
- Payment pendente de cadastro.
- Payment pendente de confirmacao.
- Duplicidade nao resolvida.
- Divergencia nao resolvida.
- Campo obrigatorio faltando em pagamento exportavel.

Nao bloqueiam por si so:

- Credito ignorado.
- Obra sem orcamento.
- Falta de OFX no periodo, quando o OFX ainda nao foi recebido. Nesse caso o sistema deve alertar.
- Pagamento aprovado sem OFX quando ainda nao ha OFX importado no periodo. Esse caso deve ficar como alerta.

Quando o checklist estiver liberado, clique em `Gerar planilhas` e baixe os arquivos de exportacao/importacao.

## O que nunca deve ser automatico

O sistema nunca deve:

- Aprovar pagamento sem confirmacao humana.
- Exportar pagamento pendente.
- Criar fornecedor/trabalhador definitivo sem confirmacao quando houver duvida.
- Sobrescrever correcao humana com sugestao de IA.
- Duplicar Payment ao reimportar o mesmo OFX.
- Tratar credito/receita como despesa.
- Inventar obra, centro de custo, categoria ou item de orcamento quando a confianca for baixa.
- Exibir token, chave, senha, `.env` ou dados sensiveis em logs, mensagens ou documentacao.

## Rotina mensal resumida

1. Lance despesas pelo Telegram ou web durante o mes.
2. No fim do mes, importe o OFX.
3. Revise `Sem lançamento`, `Pendente de cadastro`, `Pendente de confirmação`, `Possível duplicada` e `Divergente`.
4. Use edicao em lote para centro de custo, obra e categoria quando fizer sentido.
5. Aprove somente os pagamentos conferidos.
6. Abra o fechamento mensal.
7. Resolva bloqueios.
8. Gere as planilhas.
9. Envie os arquivos ao contador/sistema externo.
