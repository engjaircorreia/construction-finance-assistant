# Estrutura do repositorio

Este documento define onde cada tipo de arquivo deve ficar e o que nao deve ser versionado.

## Pastas versionadas

| Pasta | Uso |
| --- | --- |
| `apps/` | Apps Django e regras de negocio. |
| `config/` | Configuracoes Django, Celery, URLs e WSGI/ASGI. |
| `deploy/` | Scripts e configuracoes de apoio a deploy e backup. |
| `docs/` | Documentacao tecnica, operacional e historico de prompts. |
| `files/` | Somente modelos de planilha versionados. Dados reais devem ficar ignorados. |
| `static/` | Arquivos estaticos versionados, se houver. |
| `storage/` | Apenas `.gitkeep`; conteudo real e gerado deve ficar ignorado. |
| `templates/` | Templates HTML do sistema interno. |

## Pastas locais ou de VPS

| Pasta | Uso | Git |
| --- | --- | --- |
| `data/` | Area local para arquivos reais usados em importacoes manuais. | Ignorado, exceto `.gitkeep`. |
| `storage/` | Arquivos gerados, uploads, planilhas exportadas e temporarios da aplicacao. | Ignorado, exceto `.gitkeep`. |
| `backups/` ou `/backups/construction_finance_assistant` | Backups de banco e arquivos. | Ignorado. |
| `media/`, `uploads/`, `comprovantes/`, `ofx/` | Arquivos enviados por usuarios/bot. | Ignorado. |

## O que pode ficar no Git

- Codigo fonte.
- Migrations.
- Templates HTML.
- Scripts de deploy sem segredo.
- Documentacao.
- `.env.example`.
- Modelos vazios de importacao/exportacao:
  - `files/modelo_exportacao.xlsx`
  - `files/modelo_importacao.xlsx`
  - `files/planilhas_modelo_importacao/Planilha_Modelo_Pagamentos.xlsx`

## O que nao deve ficar no Git

- `.env` real.
- Token do Telegram.
- Chave da OpenAI.
- Dumps SQL.
- Backups.
- OFX reais.
- PDFs/comprovantes.
- Imagens de comprovantes.
- Planilhas reais de pagamentos.
- Cadastros reais de fornecedores/trabalhadores.
- Planilhas reais de orcamento de obra, salvo decisao explicita.
- Planilhas exportadas para contador.
- Arquivos em `storage/` gerados em runtime.

## Convencao para novos documentos

- Documentos operacionais ficam em `docs/`.
- Decisoes de arquitetura podem ficar em `docs/historico_decisoes/`.
- Prompts longos e checklists temporarios de teste nao devem ser commitados, salvo se ainda forem uteis para operar o sistema.

## Cuidados antes de commitar

Rode:

```bash
git status --short
git diff --check
```

Antes de adicionar arquivos, confira se nao ha dados reais:

```bash
git status --short --ignored
```

Se um arquivo real aparecer como `??`, ajuste `.gitignore` antes de commitar.
