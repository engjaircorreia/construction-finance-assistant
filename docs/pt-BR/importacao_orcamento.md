# Importação de Orçamento de Obra

Obras novas podem ser cadastradas pelo Telegram ou pela interface web antes de existir uma planilha de orçamento importada. Nesse caso, o sistema mostra o aviso `Obra sem orçamento importado`, mas não bloqueia o lançamento nem a exportação.

Enquanto não houver orçamento confiável para a obra, mantenha o campo `Índice Etapa / Item` vazio. O sistema não deve inventar índice de orçamento.

## Importação Pela Web

Quando a obra já estiver cadastrada, use a tela:

```text
/interno/obras/<id-da-obra>/orcamento/importar/
```

O fluxo aceita arquivo `.xlsx`, associa a planilha à obra escolhida, importa ou atualiza os itens por `obra + índice` e mostra um relatório com itens criados, atualizados, ignorados e conflitos.

Se a planilha declarar um nome de obra diferente da obra selecionada, a aba é tratada como conflito para evitar importar orçamento na obra errada.

## Importação Por Comando

A importação por comando Django continua disponível:

```bash
python manage.py import_budget_items --path "files/Orçamento Sintético - Sertãozinho.xlsx"
```

Na VPS com Docker:

```bash
docker compose -f docker-compose.shared-vps.yml exec web python manage.py import_budget_items --path "/app/files/Orçamento Sintético - Sertãozinho.xlsx"
```

É possível informar mais de uma planilha repetindo `--path`.

## Conferência

Depois de importar, confira no Django Admin se a obra possui itens de orçamento cadastrados. Ao abrir rascunhos, lançamentos ou fechamento mensal, o aviso de obra sem orçamento deixa de aparecer para obras com `BudgetItem` ativo.

## Próxima Etapa

Se necessário, a tela web pode ganhar pré-visualização antes de gravar e opção explícita para desativar itens antigos que não aparecem mais na nova planilha.
