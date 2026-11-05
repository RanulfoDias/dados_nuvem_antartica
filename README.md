# MAESTRO Drive Bridge

Python 3.12 / Windows 11. Monitora `.SPE`, preserva aquisições em SQLite e envia os arquivos SPE originais selecionados pela data de modificação para uma pasta do Google Drive. Os dados científicos ficam no Drive; opcionalmente, o Firestore recebe somente configurações operacionais e status. Não altera recursos existentes na nuvem, MAESTRO ou `.JOB`.

## Instalação e operação (PowerShell)

```powershell
.\scripts\install.ps1
notepad .\config.toml
.\.venv\Scripts\python.exe -m maestro_bridge validate .\tests\fixtures\synthetic_zero.SPE --timezone America/Sao_Paulo
.\.venv\Scripts\python.exe -m maestro_bridge offline-test
.\.venv\Scripts\python.exe -m maestro_bridge --config .\config.toml run --offline
```

Configure a pasta MAESTRO existente em `bridge.watch_directory` e seu fuso horário. Caminhos relativos são resolvidos a partir do TOML; no Windows use strings literais com aspas simples. `--offline` continua persistindo aquisições sem acessar a nuvem. `offline-test` usa banco temporário e simulador.

## Página de status no navegador

Ao executar `run` (inclusive `run --offline`), abra **http://127.0.0.1:8765** no mesmo computador. A página atualiza a cada 3 segundos e mostra atividade do monitor, acesso ao Drive, contadores da fila e horários das últimas operações. Não exibe credenciais nem conteúdo dos espectros.

Também é possível abrir apenas o painel, sem iniciar o monitor:

```powershell
.\.venv\Scripts\python.exe -m maestro_bridge --config .\config.toml ui
```

Esse comando é útil para consultar a fila com o monitor parado. Não execute `ui` e `run` com a interface embutida na mesma porta. Para um painel independente que continue aberto após parar o monitor, execute `ui` e defina `ui.enabled=false` antes de iniciar `run`. A porta é configurada em `ui.port` (padrão 8765).

“Em execução” exige atividade recente do monitor. “Acessível” exige um teste recente da pasta Google Drive, feito pelo worker aproximadamente a cada 60 segundos (ou no próximo ciclo de sincronização se maior). Isso confirma acesso à pasta naquele momento, não garante todos os uploads: consulte também erros na fila. Sem confirmação recente, a página não afirma que o Drive está conectado. Em modo offline nenhuma consulta ao Google é feita.

Se o processo HTTP parar, uma página já aberta exibe que perdeu contato e avisa que os contadores podem estar desatualizados. O servidor aceita apenas conexões locais e não fica acessível pela internet ou por outro computador. O botão de vínculo inicia OAuth em uma requisição protegida por origem local e token contra requisições de outros sites. Não requer Flask, serviços externos ou novas dependências. Para parar o painel independente, use `Ctrl+C` no terminal.

## Escolher e salvar a pasta do MAESTRO

No painel, preencha **Pasta de aquisições do MAESTRO** e clique em **Salvar pasta**. Informe o caminho da pasta de arquivos `.SPE` no computador que executa o Bridge. A pasta deve existir e permitir leitura. Caminhos relativos são resolvidos a partir do arquivo de configuração.

O caminho é salvo atomicamente em `bridge.watch_directory` no TOML em uso e permanece após reinícios. O monitor aplica a mudança no próximo ciclo, sem reiniciar e sem apagar aquisições ou envios pendentes. Se o monitor estiver parado, a pasta será usada na próxima inicialização. Uma pasta que se torne indisponível após salvar aparece como erro no estado do monitor.

## Pasta de aquisições do Sensor Arduino

No painel, selecione **Pasta de aquisições do Sensor Arduino** e clique em **Salvar pasta**. O caminho é salvo em `bridge.arduino_directory` e aplicado sem reiniciar. A pasta do MAESTRO permanece independente. Sem caminho Arduino configurado, esse monitor fica desativado.

O Arduino coleta apenas `.txt`, incluindo `.TXT`, diretamente na pasta escolhida, pela data de modificação do dia no fuso configurado. Aguarda estabilidade antes da leitura e preserva os bytes e o nome original, sem converter temperaturas, alterar codificação ou criar JSON. Usa a mesma conta Drive do MAESTRO, com horário independente: configure **Horário diário de envio do Sensor Arduino** e clique em **Salvar horário**. O valor é salvo em `bridge.arduino_upload_time`; sem esse horário, os TXT são coletados localmente, mas o envio aguarda. O horário do MAESTRO permanece em `bridge.upload_time`. Estrutura:

```
dados_do_espectrometro/sensor_arduino/dd_mm_aaaa/nome_original.txt
```

O histórico Arduino fica em um banco separado, com sufixo `-arduino.sqlite3`, ao lado do banco principal. O painel mostra seus próprios contadores e estados; os contadores superiores continuam referentes ao MAESTRO. Preserve ambos os bancos para evitar reenvios. O monitor Arduino funciona independentemente de erros de leitura na pasta MAESTRO. No modo offline, coleta localmente sem enviar. As notificações por e-mail existentes continuam referentes aos lotes do MAESTRO.

## Vincular ou trocar Google Drive pelo painel

1. Instale as dependências com `scripts/install.ps1` (ou `python -m pip install ".[drive]"`).
2. No Google Cloud, habilite a **Google Drive API**, configure o consentimento OAuth e crie um cliente do tipo **Aplicativo para computador**. Se o projeto estiver em teste, inclua sua conta entre os usuários de teste.
3. Salve o JSON do cliente em `secrets/drive-client.json`, ou informe o caminho no painel em **Configuração do Google Drive → Salvar configuração**. Esse arquivo não é uma chave Firebase nem um token de usuário; não o publique. É a única configuração inicial que o botão não consegue substituir.
4. Abra o painel e clique em **Vincular Drive**. Uma nova aba abre o login oficial do Google, com seleção de conta e consentimento. Se o navegador bloquear a aba, use **Continuar autenticação no Google** no painel.
5. Após a autorização, a aplicação cria/reutiliza `dados_do_espectrometro` no Meu Drive da conta escolhida. Mostra a conta e o estado **Conectado / Não conectado / Verificando conexão**. O teste de acesso é atualizado aproximadamente a cada minuto, inclusive no painel independente.

Para trocar de conta, clique novamente em **Vincular Drive**. Toda troca exige autenticação, inclusive se houver uma sessão Google aberta. Cancelamento, recusa ou expiração em 3 minutos preservam o vínculo anterior. O novo perfil só se torna ativo depois que credenciais e pasta estiverem prontas. O monitor em modo online adota o novo perfil no próximo ciclo, depois de terminar o lote atual. `run --offline` continua offline mesmo após vincular; inicie `run` sem a opção para enviar.

O vínculo é salvo atomicamente em `config.drive-link.json` ao lado do TOML; tokens separados por vínculo ficam na pasta do cliente OAuth. O perfil vinculado tem precedência sobre `drive.folder_id` e `drive.token_file` legados. Preserve esses arquivos fora do controle de versão e restrinja seu acesso à conta Windows executora. Tokens antigos são mantidos, não revogados automaticamente. O JSON OAuth de aplicativo ainda não foi fornecido neste ambiente: a autenticação real permanece pendente.

A autorização usa `drive.file`, com acesso aos arquivos criados/autorizados para o aplicativo, e PKCE/state no retorno local. Não solicita acesso a todo o Drive no botão. O usuário conclui consentimento na página Google; o painel não solicita senha. Login no navegador ou conexão do Drive ao chat não autentica o aplicativo Python.

O comando `drive-auth` permanece disponível para configuração legada via terminal. No novo fluxo não é necessário colar links nem escolher manualmente a pasta raiz.

Referências: [OAuth para aplicativos instalados](https://googleapis.dev/python/google-auth-oauthlib/latest/_modules/google_auth_oauthlib/flow.html), [escopos Drive](https://developers.google.com/workspace/drive/api/guides/api-specific-auth).

## Configurações e status no Firebase

Integração opcional, independente do armazenamento científico. Usa o projeto configurado em `[firebase_status]`, inicialmente `banco-de-dados-da-antartica`, e um único documento `app_status/estacao-ferraz`. Use `instance_id` diferente para cada computador. Nenhuma coleção científica existente é modificada.

O documento contém fuso e intervalos do monitor, tamanho dos lotes, ID da pasta Drive, porta da interface, estado do monitor/acesso ao Drive, contadores da fila e horários da última aquisição/sincronização. A seleção de campos é explícita: não inclui credenciais, tokens OAuth, conteúdo de arquivos, contagens espectrais, logs, e-mail da conta ou caminhos locais.

Para ativar, instale a dependência opcional, forneça o caminho da credencial de conta de serviço do projeto e defina `firebase_status.enabled=true`:

```powershell
.\.venv\Scripts\python.exe -m pip install ".[firebase]"
# Editar [firebase_status] no config.toml antes dos comandos seguintes.
.\.venv\Scripts\python.exe -m maestro_bridge --config .\config.toml publish-status
.\.venv\Scripts\python.exe -m maestro_bridge --config .\config.toml run
```

`publish-status` faz uma gravação real única, inclusive quando o monitor está parado. O envio recorrente funciona durante `run` online, numa thread separada. `run --offline` desativa também o Firebase; abrir somente `ui` não publica status. Credencial ausente/inválida ou projeto diferente são erros, nunca descoberta automática de chaves. Não deixe credenciais no repositório nem as envie pelo chat.

Por padrão, há um envio a cada 60 segundos (aproximadamente 1.440 gravações/dia por instância em execução), mais uma tentativa final ao encerrar normalmente. Não há histórico crescente: o mesmo documento é substituído. Intervalo configurável entre 60 e 3.600 segundos. Falhas não param o monitor; o próximo ciclo tenta publicar o estado atual. Não se guarda uma fila de estados antigos, pois seriam informações obsoletas.

**Como interpretar se está funcionando:** `updated_at` usa o horário do servidor Firestore. O consumidor deve comparar esse horário com o presente; se passaram mais de `stale_after_seconds` (180 segundos por padrão), exibir **Sem comunicação recente**, mesmo que o último campo `monitor` seja `running`. `running` recente significa monitor ativo; `stopped` significa encerramento conhecido; `error` indica problema na pasta local; `stale` indica ausência de atividade recente do monitor. Falta de comunicação pode ser internet ausente, computador desligado ou aplicativo parado — não permite distinguir essas causas remotamente. `reported_state` em `telemetry.py` implementa essa regra. Não habilite exclusão TTL nesse campo: ele é um limite de interpretação, não uma solicitação de apagamento.

Essa integração prepara a consulta remota do estado, mas não cria dashboard remoto, notificações ou alarmes. A autorização Google Drive e a credencial Firebase são separadas. No ambiente atual a gravação real está desativada por ausência de credencial explícita.

## Notificações de backup por e-mail

No painel, preencha **E-mail que receberá as notificações** e clique em **Salvar e-mail**. Em **Configurar servidor de envio (SMTP)**, informe servidor, porta, SSL/TLS ou STARTTLS, remetente, usuário e senha SMTP (ou senha de aplicativo fornecida pelo provedor). Clique em **Salvar servidor**. Salvar valida e persiste os campos; não testa autenticação nem garante entrega. O painel informa que o servidor ainda precisa ser configurado quando necessário.

O backup neste projeto é a cópia dos arquivos SPE no Google Drive. Uma notificação é criada por lote confirmado, somente depois de os arquivos SPE serem enviados e a confirmação local ser persistida. Não há e-mail por atualização visual do painel nem por tentativa de upload com falha. O resumo contém quantidade de aquisições, horário UTC e link da pasta; não inclui espectros, anexos, credenciais ou caminhos locais. Não há envio retroativo de lotes concluídos antes de salvar o destinatário.

As configurações ficam em `secrets/<nome-do-config>-notifications.json`, ao lado da configuração utilizada, com gravação atômica. A senha é armazenada nesse arquivo local e nunca retornada pela API do painel; no POSIX, a permissão do arquivo é 0600. No Windows, restrinja o acesso à pasta à conta executora. O diretório `secrets/` já está excluído do controle de versão. Campo de senha vazio preserva a senha existente apenas para o mesmo servidor e usuário.

As notificações ficam em uma fila SQLite persistente, criada na mesma transação da confirmação do backup. Falhas SMTP não desfazem o backup nem exigem reenvio dos espectros. A tentativa é repetida de 30 segundos até 1 hora de intervalo, respeitando também o ciclo de sincronização. As configurações são relidas durante a execução. Ao desativar ou mudar o destinatário, notificações pendentes para o endereço anterior são canceladas no próximo ciclo online; um envio já iniciado pode terminar. Salvar vazio desativa as notificações.

Somente `run` online envia e-mails. `run --offline` e `ui` isolado permitem configurar os campos, mas não enviam. O painel mostra pendências, falhas e a última aceitação pelo servidor SMTP; aceitação não garante chegada à caixa de entrada. Em caso de interrupção após o servidor aceitar uma mensagem mas antes da confirmação local, uma repetição é possível; o identificador da mensagem é mantido na nova tentativa.

## Envio diário em horário configurável

No painel, escolha **Horário diário de envio** e clique em **Salvar horário**. A preferência é salva em `bridge.upload_time` no TOML (`HH:MM`) e aplicada no próximo ciclo, sem reiniciar. Sem horário configurado, nenhum novo upload é iniciado. Um lote já em andamento termina antes de aplicar uma alteração.

Todos os dias, a partir do horário escolhido, o programa envia arquivos `.spe` (sem diferenciar maiúsculas e minúsculas) modificados no próprio dia, diretamente na pasta de origem. Usa o fuso `bridge.acquisition_timezone`, atualmente `America/Sao_Paulo`. A seleção do dia avança automaticamente à meia-noite, substituindo a seleção manual fixa. Arquivos novos desse dia que aparecerem após o horário também são enviados, sem repetir conteúdos já confirmados. O intervalo de verificação segue `sync_seconds` (30 segundos por padrão).

O computador e o programa precisam permanecer ligados em modo online. Se iniciar depois do horário, o programa envia os arquivos pendentes do dia no próximo ciclo. Falhas são tentadas novamente após o horário enquanto a data ainda corresponde ao dia atual. Arquivos de dias anteriores não são enviados automaticamente. A validação científica e a espera pela estabilidade do arquivo continuam em vigor.

Somente o arquivo SPE original é enviado, mantendo bytes, nome e extensão originais:

```
dados_do_espectrometro/
  26_10_2026/
    C01_N001.Spe
```

Não são criados novos arquivos JSON no Drive. Os metadados continuam no banco local para validação. Arquivos e pastas enviados antes desta mudança permanecem como estão, incluindo JSONs antigos; não são apagados nem reenviados automaticamente.

A fila limita o envio à data selecionada, inclusive para aquisições pendentes. SHA-256 e IDs remotos persistidos evitam duplicação após falha ou reinício: um conteúdo já confirmado para o destino não é reenviado. Arquivos com nomes iguais e conteúdos distintos mantêm IDs distintos. Preserve o banco SQLite para preservar esse histórico. Uma alteração no conteúdo é uma nova aquisição.

O programa confirma o upload do SPE antes de registrar a conclusão. Em resposta perdida, usa o mesmo ID e verifica tamanho, SHA-256 e pasta no Drive. Falhas mantêm a aquisição pendente para nova tentativa. Lotes têm até 100 aquisições, sem transação atômica no Drive.

## Persistência e migração

SQLite usa WAL/FULL; bytes originais e JSON são salvos antes do envio. O banco inclui o mapeamento dos IDs Drive e deve ser preservado. Na primeira conexão validada ao Drive, registros anteriormente confirmados pelo Firebase voltam à fila para migração. Os recursos Firebase existentes não são excluídos nem alterados.

Cada pasta raiz autenticada tem seu histórico de envio e seus IDs remotos preservados no mesmo banco. Uma conta nova recebe as aquisições locais ainda não enviadas para ela; nenhum arquivo é removido da conta anterior. Ao voltar à conta/pasta conhecida, aquisições já confirmadas não são reenviadas. Trocas aguardam uploads ativos concluírem; workers antigos não podem reservar novos itens para um destino desativado. Não execute vários monitores sobre o mesmo banco.

Arquivos já confirmados no layout antigo permanecem no local original. O novo layout se aplica aos próximos uploads. Arquivos legados parcialmente enviados podem exigir análise dos logs antes de reprocessar; arquivos existentes não são sobrescritos em caso de conflito de integridade. Perder o banco/mapeamento pode causar duplicatas numa reimportação.

Recuo exponencial de 5 s até 1 h; reservas expiram em 600 s após interrupção. Tokens de reserva impedem confirmação por um worker antigo. Uploads rodam em thread separada do monitor. Logs rotativos registram rejeições e falhas. Não há limpeza automática de dados; acompanhe o espaço local. `Ctrl+C` aguarda o envio em andamento.

## Ciência e leitura

- Detectores físicos: 1, 2, 3, 4, 5, 7, lidos de `SPEC_REM / DET#`; mapeamento explícito por nome exato pode complementar arquivos sem ID, mas conflitos são rejeitados.
- `$DATE_MEA`: `MM/DD/YYYY HH:MM:SS`, no fuso configurado; ambiguidades exigem `ambiguous_fold`. Não se usa mtime como horário científico.
- `$MEAS_TIM`: tempo vivo e real, positivos, finitos e vivo ≤ real. Nenhuma duração fixa; aquisições acima de 600 s são aceitas.
- Total `N=sum(contagens)`, incerteza estimada de Poisson `sqrt(N)`, taxas `N/t` e incertezas `sqrt(N)/t` para ambos os tempos. Para zero contagens, o estimador é zero, não um intervalo de confiança ou limite superior. Não inventamos eficiência, fundo, fluxo ou unidades físicas.
- Polling não recursivo; tamanho/mtime/ctime/identidade devem ficar estáveis e são conferidos após leitura. Canais declarados e seções conhecidas são validados. Escrita pausada num trecho sintaticamente completo ainda pode parecer concluída: ajuste a estabilidade e preserve nomes únicos por aquisição. Sobrescritas mais rápidas que o polling podem ser perdidas.
- UTF-8/CP1252, arquivos até 64 MiB. Arquivos inválidos são registrados e reavaliados quando mudam ou após reinício.

## Inicialização automática no macOS, Linux e Windows

No painel, use **Iniciar com o sistema**, marque **Ativar inicialização automática**, escolha modo offline ou online e clique em **Salvar inicialização**. A configuração vale para o próximo login e não inicia uma segunda instância imediatamente. Desmarcar e salvar remove a inicialização futura, sem parar o monitor atual. O modo online exige um vínculo Drive ou `drive.enabled=true` previamente configurado.

- macOS: LaunchAgent em `~/Library/LaunchAgents/br.ferraz.bridge.<id>.plist`.
- Linux: entrada XDG em `$XDG_CONFIG_HOME/autostart/` ou `~/.config/autostart/`; requer uma sessão gráfica que implemente XDG Autostart.
- Windows: entrada da conta atual em `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`.

Não requer administrador. A execução começa após login, não antes da autenticação do usuário. Não é um serviço de boot para Linux sem ambiente gráfico. O monitor inicia em segundo plano; abra a URL do painel para consultá-lo. Permissões ou políticas do sistema podem bloquear itens de login.

Os registros guardam caminhos absolutos do Python, do inicializador e da configuração. Preserve essas localizações; se mover o projeto, desative antes e reative no novo local. Cada configuração tem um identificador próprio. Não mantenha simultaneamente uma tarefa legada criada por `register-task.ps1`. Uma trava do sistema operacional impede monitores novos de compartilharem o mesmo banco. A trava é liberada ao encerrar, inclusive após falha; o arquivo `.lock` pode permanecer no disco. A configuração da inicialização não altera o modo do processo atual.

## Windows e testes

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
.\scripts\register-task.ps1          # agenda modo offline no login atual
# Depois da autenticação e configuração:
# .\scripts\register-task.ps1 -Online
Start-ScheduledTask -TaskName 'MAESTRO Drive Bridge'
```

A tarefa roda após login sob a conta atual e não armazena senha. Remova a antiga tarefa `MAESTRO Firebase Bridge`, se existir, para não manter dois monitores. Scripts não alteram políticas PowerShell.

Camadas: `science.py` parser, `local.py` monitor/SQLite, `sync.py` coordenação, `drive.py` OAuth/API, `cli.py` comandos, `link.py` vínculo OAuth e `ui.py`/`status.html` interface local. Testes não usam credenciais reais: seis detectores, zero/positivos, 600/900/1800 s, truncamento, corrupção, cópias, reinício, indisponibilidade, falha parcial e resposta de upload perdida.

`AGENTS.md`, `SPEC.md` e fixtures originais não estavam disponíveis. A fixture incluída é sintética. Testes locais executados em Linux/Python 3.13; validar também no Windows 11/Python 3.12 e com espectros originais antes da operação científica.

Ambiente local macOS: use `.venv-macos/bin/python -m maestro_bridge --config config.macos.toml run --offline`. Após vincular e quando quiser enviar, execute sem `--offline`. As dependências Google estão instaladas nesse ambiente virtual.

## Arquivo diário consolidado do Arduino

O Arduino mantém um único TXT por data de modificação, dentro de `sensor_arduino/dd_mm_aaaa`. Preserva o nome do primeiro arquivo do dia e reutiliza o mesmo ID do Drive nas atualizações. Versões locais e outros TXT do mesmo dia são consolidados com um cabeçalho `DATA_HORA;...`; linhas de leitura exatamente iguais aparecem uma vez. Leituras com valores diferentes são preservadas, inclusive no mesmo instante. Colunas incompatíveis interrompem o envio para evitar mistura de formatos.

Após verificar o SHA-256 do arquivo consolidado, cópias antigas criadas pelo aplicativo no mesmo dia são movidas para a lixeira do Drive, somente se ainda correspondem ao conteúdo registrado localmente. Os TXT originais no computador não são alterados. A atualização diária segue o horário Arduino configurado. O histórico interno preserva as versões recebidas; os contadores representam essas versões, não a quantidade de arquivos finais no Drive.
