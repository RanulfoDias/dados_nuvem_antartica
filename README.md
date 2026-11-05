# Monitor de aquisições científicas do OPD

Variante para o Observatório do Pico dos Dias (OPD), baseada no mesmo fluxo de aquisição usado no projeto da Antártica. Monitora arquivos de espectrometria `.SPE`, mantém uma fila local recuperável em SQLite e pode enviar os arquivos originais e seus metadados científicos em JSON para uma pasta Google Drive. Também inclui monitor opcional de arquivos `.TXT` do Sensor Arduino, painel local, agenda de sincronização, notificações por e-mail e integração operacional opcional com Firebase.

O modo offline mantém as aquisições no computador e não acessa serviços em nuvem. A integração com Google Drive e Firebase vem desativada no exemplo; configure uma conta e um destino próprios do OPD antes de ativá-las. O vínculo com a pasta existente pede acesso amplo de leitura e gravação ao Google Drive; o perfil OAuth e o token do OPD ficam separados. Não reutilize o `config.toml` existente, banco de dados, token OAuth ou credenciais do projeto da Antártica; use o novo `config-opd.toml` e credenciais independentes.

## Requisitos e instalação

Requer Python 3.12 ou superior. No Linux/macOS:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[drive]'
cp config.example.toml config-opd.toml
```

No Windows PowerShell:

```powershell
.\scripts\install.ps1
Copy-Item .\config.example.toml .\config-opd.toml
```

Edite `config-opd.toml` e selecione em `bridge.watch_directory` a pasta de espectros efetivamente usada no OPD. O exemplo usa `incoming`. Use este arquivo novo e mantenha separadas as configurações e credenciais de outros ambientes que já existam nesta pasta. Caminhos relativos são resolvidos a partir do arquivo de configuração. Configure também o fuso horário de aquisição, normalmente `America/Sao_Paulo`.

## Validar e iniciar

Valide um espectro existente:

```bash
python -m maestro_bridge --config config-opd.toml validate /caminho/para/arquivo.SPE --timezone America/Sao_Paulo
```

Para iniciar sem nuvem:

```bash
python -m maestro_bridge --config config-opd.toml run --offline
```

O processamento mantém a aquisição local até a integração com Drive ser configurada. Para sincronizar, instale o extra `drive`, configure OAuth com credenciais da conta do OPD, selecione uma pasta de destino própria e então inicie em modo online:

```bash
python -m maestro_bridge --config config-opd.toml run
```

Os arquivos `.SPE` da estação seguem para `dados_opd/dd_mm_aaaa` no destino Google Drive. Os dados do Sensor Arduino da estação OPD seguem para `dados_opd/sensor_arduino/dd_mm_aaaa`. O token OAuth e as credenciais devem permanecer em `secrets/`, que é ignorada pelo Git. Não inclua credenciais ou tokens no repositório.

## Painel local

Com o monitor em execução, abra **http://127.0.0.1:8765** neste computador. O painel mostra o estado do monitor, da conexão com o Drive, contadores e operações recentes. Ele também permite escolher as pastas monitoradas e configurar agendamento, inicialização automática e notificações.

Para abrir o painel separadamente:

```bash
python -m maestro_bridge --config config-opd.toml ui
```

O servidor aceita apenas conexões locais. Para desativá-lo, ajuste `ui.enabled = false` no arquivo de configuração.

## Sensor Arduino (opcional)

O monitor Arduino é independente da pasta do espectrômetro. No painel, escolha a pasta de arquivos `.TXT` e, se desejar, agende o envio diário. A sincronização reutiliza a conta e a pasta configuradas para o Drive, preservando os arquivos originais.

## Firebase (opcional)

A integração publica somente status operacional; não envia espectros nem JSON científicos ao Firebase. Para ativá-la, instale `.[firebase]` e configure um projeto Firebase exclusivo ou autorizado para o OPD, com `project_id`, `instance_id = 'opd'` e uma credencial de conta de serviço protegida em `secrets/`. Mantenha `enabled = false` até preencher esses dados.

## Marcas institucionais

O rodapé usa um painel ilustrativo baseado na fotografia fornecida. Os logos do IEAv e ERISA-D enviados pelo proprietário são exibidos sobre o painel; as demais marcas são uma reconstrução visual e devem ser substituídas pelos arquivos oficiais aprovados pelas respectivas instituições antes de uso institucional/publicação. As marcas PROANTAR, EACF/Ferraz e Marinha foram retiradas desta variante.
