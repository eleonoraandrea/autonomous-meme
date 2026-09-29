# MEME-RL-SOL — piano operativo

**Stato:** piano di riferimento, 29 settembre 2026.  
**Origine:** `progetto.md`. Questo piano restringe la prima implementazione; non sostituisce la visione di lungo periodo.  
**Obiettivo della prima versione:** misurare, senza usare denaro reale, se segnali osservabili in tempo utile sui nuovi token Solana producono decisioni di trading migliori del non operare e di baseline semplici, **dopo** costi ed errori di esecuzione.

## 1. Decisioni vincolanti

1. **Nessun trading live nel MVP.** Il processo paper non possiede chiavi, accesso al signer o un percorso di invio transazioni. `MODE=paper` da solo non è una barriera di sicurezza.
2. **Long spot soltanto.** L'azione iniziale è `flat` oppure un'esposizione long limitata; lo short `-1` di `progetto.md` non è previsto dall'esecuzione spot descritta.
3. **Dati e simulazione prima dei modelli.** Si implementano prima ingestione, replay e baseline deterministiche; ML supervisionato e RL sono esperimenti successivi, non dipendenze per un prodotto funzionante.
4. **Risk engine indipendente.** Qualunque strategia produce soltanto un *trade intent*. Controlli deterministici possono rifiutarlo o ridurne la size; nessun modello può modificare i limiti o accedere a credenziali.
5. **Decisioni point-in-time.** Una feature è utilizzabile soltanto dal momento in cui era realmente disponibile al sistema, non dal timestamp retrodatato dell'evento o dalla successiva ricostruzione storica.
6. **Ogni fase ha un gate.** Un risultato negativo, dati insufficienti o costi non sostenibili possono chiudere il progetto prima del live. Il passaggio di fase non è automatico.
7. **Nessuna promessa di rendimento o sicurezza dei token.** Le soglie di rischio e le stime economiche sono ipotesi da validare; non garantiscono l'uscita da posizioni illiquide o il rispetto di uno stop.

## 2. Perimetro

**MVP incluso:** discovery di token/pool; acquisizione e conservazione degli eventi; dati di mercato e liquidità; poche verifiche on-chain; data quality e risk flags; baseline; backtester a replay; paper trading live; report e alert operativi.

**Escluso dal MVP:** firma e invio di transazioni, PPO/SAC, score social multi-piattaforma, reputazione complessa dei wallet, Qdrant, LLM agentici, x402, API commerciale, microservizi separati per ogni componente. Birdeye è una fonte di verifica opzionale entro budget, non una dipendenza obbligatoria per ogni token.

**Assunzioni da verificare nella fase 0:** accesso lecito alle API/feed scelti e loro limiti effettivi; possibilità di ottenere eventi di nuove pool con latenza utile; prezzi e profondità/quote sufficienti per simulare entrata e uscita; disponibilità e costo della storia necessaria al replay. Non si assumono validi i numeri dei piani gratuiti o le capacità dei provider riportati in `progetto.md` senza una nuova verifica.

## 3. Architettura minima

```text
provider / Solana RPC
        ↓
collector + rate limiter + raw event log
        ↓
normalizzazione, deduplica, data-quality checks
        ↓
feature point-in-time + token state machine
        ↓
hard risk filter → baseline/model → trade intent
        ↓
portfolio/risk engine → paper execution → ledger
        ↓
replay, metriche, alert, report
```

**Implementazione iniziale:** una codebase Python con moduli separati, PostgreSQL e Docker Compose. Job/worker distinti solo dove servono; Redis Streams soltanto se il carico e la necessità di replay/consumatori indipendenti lo giustificano. Un log append-only persistente in PostgreSQL è sufficiente per iniziare. TimescaleDB si aggiunge se volume e query sui tick lo richiedono. L'adapter TypeScript/Jupiter e un signer isolato sono lavori **solo della fase live**, non del MVP. Si preferiscono componenti piccoli e osservabili a un avvio con venti container.

### Contratto minimo degli eventi

Ogni record deve includere almeno `event_id` idempotente, `chain`, `token_mint`, eventuale `pool_address`, `source`, `source_event_time`, `provider_publish_time` se noto, `received_at`, `ingested_at`, payload grezzo, versione dello schema e qualità/affidabilità. Distinguere chiaramente `null`/non disponibile da zero. Salvare la risposta originale quando consentito dai termini del provider.

Ogni decisione registra: versione di feature e strategia, dati effettivamente visibili, età delle feature, esito dei controlli di rischio, quote/costi stimati, azione proposta, azione ammessa e motivazione del rifiuto. Gli stati dei token sono `DISCOVERED → OBSERVED → ELIGIBLE → WATCHED → BLOCKED/EXPIRED`; le transizioni sono tracciate, non inferite a posteriori.

### Contratto paper e replay

Il backtester e il paper trader consumano la **stessa interfaccia di decisione e rischio**. Il replay rilascia eventi in ordine di disponibilità (`received_at` o primo timestamp di effettiva disponibilità ricostruibile), mai per il solo tempo on-chain. Gli ordini paper distinguono `intent`, `submitted`, `filled`, `partially_filled`, `failed`, `cancelled` e `expired`. Includono bid/ask o quote eseguibili quando disponibili, price impact dipendente dalla size, commissioni, fee di rete/priorità, ritardo decisionale e casi senza fill. Se mancano i dati per stimare l'uscita, il trade non può essere etichettato come profitto realizzabile.

## 4. Sequenza di lavoro e gate

| Fase | Deliverable | Criterio per procedere |
| --- | --- | --- |
| **0 — Fattibilità dati** | Matrice provider/endpoints/termini/costi/limiti; prototipo di discovery e raccolta campione; misure di copertura, duplicati, latenza e ritardi per fonte; inventario dei dati storici. | È documentato **quali** segnali sono disponibili prima della decisione, a che età e per quali token. Se la latenza reale supera l'orizzonte del segnale, si elimina la strategia early-launch oppure ci si ferma: non si compensa con un backtest retrodatato. |
| **1 — Data collector** | Ingestione resiliente, normalizzazione, idempotenza, log raw append-only, rate limiting e backoff per provider, metriche di freschezza e fallback espliciti. | Replay deterministico dello stesso campione; nessun evento duplicato produce doppie azioni; dati mancanti o stale bloccano la feature interessata. Test di riavvio e di quota/rate-limit superati. |
| **2 — Risk + baseline** | Risk flags verificabili, limiti di esposizione, ledger paper, strategia `no trade` e 1–2 baseline semplici (per esempio momentum con filtro liquidità), senza ML. | Ogni intent ha un verdetto motivato; test automatici provano che dati stale, size eccessiva, condizioni di uscita non stimabili e perdita oltre limite impediscono nuove entrate. |
| **3 — Replay e validazione** | Backtester event-driven, modello conservativo dei fill, coorte di token completa, report con costi e scenari pessimistici, split cronologici. | Audit manuale di un campione di episodi: nessuna feature futura, niente soli token sopravvissuti, risultati riproducibili e confronto netto con `no trade` e baseline. Se non si riesce a ricostruire la disponibilità storica, usare soltanto dati raccolti prospetticamente. |
| **4 — Paper live** | Operatività continua senza signer, dashboard minima, alert, riconciliazione degli ordini simulati con quote/mercato osservati e incident log. | Periodo e campione prestabiliti prima del test (proposta: almeno 30 giorni **e** 100 decisioni indipendenti, estendibili se insufficienti); costi realistici; edge netto robusto a ipotesi peggiori di slippage/ritardi; limiti di rischio rispettati; nessuna anomalia critica irrisolta. Non è un'autorizzazione automatica al live. |
| **5 — Modelli opzionali** | Wallet intelligence limitata e/o modello supervisionato calibrato; RL soltanto se baseline e simulatore hanno superato i gate. | Miglioramento out-of-sample rispetto alla baseline su periodi walk-forward e più regimi, al netto dei costi, con intervalli di incertezza; nessun peggioramento materiale del rischio di coda. Altrimenti restare sulla baseline o non operare. |
| **6 — Shadow e live sperimentale** | Adapter quote/esecuzione, signer isolato, reconciliation on-chain, kill switch, approvazione umana, shadow e poi capitale limitato. | Checklist di sicurezza e conformità completata; scostamento paper-vs-quote/live misurato; autorizzazione umana esplicita per ogni aumento del capitale. Qualsiasi incidente blocca nuovi ordini finché analizzato. |

Le fasi 0–4 costituiscono il **primo rilascio utile**. Le fasi 5–6 sono condizionali: non vengono pianificate come conseguenza inevitabile delle precedenti.

## 5. Misure e criteri sperimentali

Prima di ogni test si congelano strategia, parametri, universo, periodo, regole di esclusione e soglie; modificarli significa avviare un nuovo test. Si pubblicano anche i risultati negativi.

- **Dati:** quota di nuove pool rilevate rispetto a un campione di riferimento; distribuzione p50/p95/p99 del ritardo pool→evento disponibile; percentuale di feature mancanti o stale; divergenza tra fonti. Si misurano per provider e fascia oraria. I target di latenza si fissano **dopo** la fase 0 per ciascun orizzonte: nessuna promessa generica di `<500 ms`.
- **Selezione:** quanti token sono visti, esclusi, eleggibili e negoziabili; includere esplicitamente token morti/rug e casi non quotabili. Le metriche non devono considerare solo il «top 20» restituito dall'API.
- **Strategia:** P&L netto realizzabile, turnover, max drawdown, perdita di coda, esposizione e concentrazione, frequenza di mancati fill, risultati per regime e per coorte temporale. Distinguere rendimento mark-to-market da rendimento effettivamente liquidabile.
- **Predizioni, se introdotte:** etichetta operativa e orizzonte definiti; calibrazione e qualità delle probabilità; confronto con baseline; drift. `P(rug)` non viene mostrata come probabilità precisa finché non esistono etichette e calibrazione affidabili.
- **Promozione:** richiedere evidenza prospettica e robustezza a costi/ritardi peggiori, non solo un punto stimato positivo. Se campione o intervalli di incertezza non permettono una conclusione, il gate rimane chiuso.

La wallet reputation richiede attribuzione affidabile del costo di acquisto e delle vendite; transfer, wallet collegati e dati mancanti possono invalidare il P&L apparente. La partecipazione di più wallet correlati non prova identità comune né «alpha». Social lead time richiede timestamp di **prima osservazione**, non l'ora del post recuperato dopo il movimento di prezzo.

## 6. Sicurezza, capitale e operazioni

**Paper:** nessun segreto di firma distribuito, nessuna dipendenza di rete che possa inviare transazioni, test che falliscono se viene introdotto un percorso verso `sendTransaction`. Fail closed su sorgenti assenti/stale o stato del ledger incerto. Pausa manuale sempre disponibile.

**Solo per un eventuale live:** hot wallet separato e finanziato con un importo sacrificabile; cold wallet non raggiungibile; signer con autorizzazione limitata e policy indipendente; allowlist di programmi/destinazioni dove applicabile; verifica mint, route, amount, min-out/slippage e saldo prima di firmare; simulazione quando disponibile; idempotenza degli intent; conferma e riconciliazione on-chain prima di aggiornare la posizione. Quote e simulazione non garantiscono il fill né la possibilità di uscire. Nessun retry cieco degli swap.

I limiti iniziali sono **configurazioni sperimentali da approvare**, non raccomandazioni finanziarie: massima size per posizione e per liquidità disponibile, impatto/slippage massimo, esposizione aggregata, budget fee e perdite giornaliere/complessive. Si blocca l'apertura di nuove posizioni quando scatta un limite; gli stop potrebbero non essere eseguibili al prezzo desiderato. Conservare audit trail e procedura di arresto/ripristino. Prima del live verificare obblighi normativi, fiscali e termini di utilizzo delle fonti dati nella giurisdizione interessata.

## 7. Evoluzione dopo evidenza positiva

1. **Wallet intelligence mirata:** seguire solo pochi wallet e token con storia ricostruibile; confrontare contro segnali casuali e correggere per survivorship/selection bias.
2. **Supervised learning:** pochi target definiti e probabilità calibrate; modello usato come feature/ordinamento, non come autorizzazione a negoziare.
3. **RL per gestione posizione:** ambiente validato contro paper live; stato point-in-time; azioni long-only; reward netto dei costi; confronto con regole fisse. PPO/SAC si scelgono in base all'ambiente e alle prove, non in anticipo.
4. **Social intelligence:** soltanto con accesso consentito, copertura misurata e timestamp di ricezione; altrimenti escluderla.
5. **x402/API commerciale:** progetto separato, dopo aver dimostrato domanda e qualità del dato. Budget/provider allowlist, limiti di spesa e verifica del valore informativo prima degli acquisti automatici. Nessun processo di ricerca o modello ottiene un wallet di pagamento senza controlli indipendenti.

Qdrant, TimescaleDB, Redis Streams, Grafana e una decomposizione in microservizi sono strumenti possibili, **non requisiti di partenza**. Si adottano quando una metrica operativa dimostra che il componente più semplice non basta.

## 8. Primo backlog eseguibile

1. Documentare provider candidati, endpoint, ToS, costi, rate limit e dati storici realmente accessibili; decidere il campione di riferimento per la copertura della discovery.
2. Definire schemi versionati di evento, token/pool, feature, intent, verdict, ordine paper e ledger; definire test point-in-time e idempotenza.
3. Creare collector minimale e campagna prospettica di misurazione; conservare input raw e metriche di lag prima di sviluppare qualunque strategia.
4. Formalizzare le baseline, le regole di rischio configurabili e il protocollo sperimentale congelato.
5. Costruire replay e paper trader con lo stesso decision/risk path; verificare manualmente alcuni trade completi, comprese uscite fallite.
6. Eseguire paper live, pubblicare report e decidere **stop / iterazione / fase successiva** con evidenza registrata.

**Definizione di successo del primo rilascio:** il sistema produce dati auditabili, decisioni riproducibili e una valutazione onesta della fattibilità. È un successo anche dimostrare che le fonti o i costi disponibili **non** consentono una strategia profittevole sui token appena lanciati, evitando di esporre capitale reale a una premessa non verificata.
