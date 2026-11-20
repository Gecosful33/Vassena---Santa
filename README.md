# Calendario case

Calendario unico delle case in affitto breve, alimentato ogni notte dai portali
proprietari dei gestori (Krossbooking) e pubblicato come pagina web cifrata.

- **La famiglia** apre la pagina con una password e vede, per ogni casa, quando ci sono
  ospiti, quando le date sono bloccate e quando è libera.
- **La proprietà** ha una seconda password che sblocca anche ricavi, commissioni e
  netto, mese per mese e prenotazione per prenotazione.

La pagina è raggiungibile da chiunque, ma contiene solo testo cifrato: senza password
non si legge nulla, nemmeno i nomi delle case.

## Come funziona

```
ogni notte (GitHub Actions, 02:17 UTC)
  └─ per ogni casa: login al portale → statistiche dell'anno in corso e del successivo
     → elenco prenotazioni → logout
  └─ controlli di coerenza (vedi sotto)
  └─ cifratura a due livelli → una sola pagina HTML → GitHub Pages
```

È uno script deterministico, non un agente: nessun modello di linguaggio gira di
notte. Nel repository non viene scritto alcun dato; la pagina è un artefatto di deploy.

## Messa in funzione

Da fare una volta sola, nell'ordine.

### 1. Inserire gli 8 secret

`Settings → Secrets and variables → Actions → New repository secret`, uno per riga:

| Nome | Valore |
|---|---|
| `SANTA_KB_URL` | indirizzo del portale del gestore, es. `https://vr.krossbooking.com/<gestore>` |
| `SANTA_KB_USER` | utente del portale |
| `SANTA_KB_PASS` | password del portale |
| `VASSENA_KB_URL` | come sopra, per la seconda casa |
| `VASSENA_KB_USER` | utente del portale |
| `VASSENA_KB_PASS` | password del portale |
| `FAMILY_PASSPHRASE` | password di famiglia, almeno 16 caratteri |
| `OWNER_PASSPHRASE` | password della proprietà, almeno 16 caratteri, diversa dalla precedente |

Va bene incollare l'indirizzo della pagina su cui si atterra dopo il login
(`…/<gestore>/property`): conta solo la parte fino al nome del gestore.

Le due password proteggono un testo cifrato che chiunque può scaricare e attaccare
con calma: la lunghezza è l'unica difesa. Quattro o cinque parole a caso separate da
un trattino vanno bene; `python -m casecal passphrase` ne genera una. Il programma
rifiuta password corte, ripetitive o uguali a quelle dei portali.

### 2. Attivare GitHub Pages

`Settings → Pages → Build and deployment → Source: GitHub Actions`.

### 3. Portare il codice sul ramo `main`

I workflow schedulati partono solo dal ramo predefinito.

### 4. Primo avvio

`Actions → Aggiornamento notturno → Run workflow`. Al termine, l'indirizzo della pagina
compare nel riepilogo del job (`https://<utente>.github.io/<repository>/`).

### 5. Condividere

Indirizzo e password di famiglia a chi deve vedere il calendario. La password della
proprietà solo a chi deve vedere i conti.

## Cosa mostra la pagina

| | Password di famiglia | Password della proprietà |
|---|---|---|
| Calendario unico delle case, stato di stanotte, prossimi periodi liberi | sì | sì |
| Elenco soggiorni e blocchi, con canale e numero di ospiti | sì | sì |
| Statistiche mensili, grafico del netto, dettaglio economico per prenotazione | no | sì |

### Come si legge il calendario

I giorni sono tessere e ogni soggiorno è una barra arrotondata stesa sopra di esse,
come nel calendario dell'app di Airbnb. La barra è in scala con l'orologio: **inizia
all'ora di check-in del primo giorno e finisce all'ora di check-out dell'ultimo**
(15:00 e 11:00). Si vede quindi a colpo d'occhio che il giorno di arrivo è libero fino
al pomeriggio e quello di partenza lo torna dalla tarda mattinata; in un cambio in
giornata le due barre non si toccano e resta scoperto l'intervallo fra uscita e
ingresso.

Ogni barra porta la sigla della casa e, se c'è spazio, il numero di ospiti e il canale;
un testo che non entra non viene scritto (resta nel riquadro che si apre toccando il
giorno e nella tabella dei soggiorni). Le barre piene sono soggiorni di ospiti, quelle
chiare date bloccate. Gli stessi orari compaiono per esteso nel riquadro del giorno,
nelle schede «stanotte» e nelle tabelle.

Gli orari si impostano in `properties.toml` con `check_in` e `check_out`: valgono per
tutte le case, e una casa con orari diversi li ridefinisce nel proprio blocco.

Tre stati, con lo stesso nome ovunque:

- **Ospiti**: c'è una prenotazione.
- **Bloccata**: date chiuse alla vendita dal proprietario o dal gestore, senza ospiti.
- **Libera**: nessuna prenotazione e nessun blocco *all'ultimo aggiornamento*. Resta in
  vendita: può essere prenotata in qualsiasi momento. Prima di organizzarsi vanno fatte
  bloccare le date.

I nomi degli ospiti e le nazionalità non vengono mai letti dal portale: il programma
estrae solo date, canale, numero di ospiti e colonne di importi.

Gli importi sono quelli dei portali, con le colonne che ciascun gestore ha
configurato (possono differire da casa a casa). Le uniche grandezze calcolate sono
marcate con un asterisco: lordo = notti × ADR, e il rapporto netto/lordo.

## Sicurezza: cosa è protetto e cosa no

- **Cifratura.** Chiavi derivate con PBKDF2-HMAC-SHA256 (600.000 iterazioni), contenuti
  cifrati con AES-256-GCM. Il browser decifra in locale con WebCrypto; la password non
  lascia mai il dispositivo.
- **Due livelli.** Il blocco della proprietà contiene la chiave di famiglia: con la
  password della proprietà si apre tutto, con quella di famiglia solo il calendario.
- **Nessuna connessione in uscita.** La pagina è un unico file con una
  Content-Security-Policy che ammette solo il proprio script e il proprio stile (per
  hash) e nessuna richiesta di rete.
- **«Ricorda su questo dispositivo»** salva nel browser la chiave derivata, non la
  password. «Blocca» la cancella. Non usarlo su dispositivi condivisi.
- **Log pubblici.** Il repository è pubblico, quindi lo sono i log di Actions: il
  programma stampa solo l'esito per casa e il tipo di errore.

Limiti da conoscere:

- Chi ha la password di famiglia vede il calendario finché la password non cambia. Per
  revocare un accesso bisogna cambiare `FAMILY_PASSPHRASE` e ridistribuirla; al run
  successivo le chiavi ricordate sui dispositivi smettono di funzionare.
- Una password debole si indovina offline. Non riutilizzare password esistenti.
- Le credenziali dei portali stanno nei secret di GitHub: chi ha accesso in scrittura
  al repository può farle uscire modificando un workflow. Tenere il repository
  personale e l'account GitHub protetto da autenticazione a due fattori.

## Controlli di coerenza

Il portale non è nostro e può cambiare senza preavviso. Un parser che prosegue su una
pagina che non capisce più pubblicherebbe un calendario sbagliato, quindi ogni lettura
viene verificata e, al primo dubbio, scartata:

- la pagina deve dichiarare l'anno e l'appartamento richiesti;
- la griglia annuale deve avere 12 mesi di 31 celle e marcare come inesistenti
  esattamente i giorni che non esistono (30 febbraio, 31 aprile, 29 febbraio negli
  anni non bisestili);
- ogni soggiorno deve avere i marcatori di arrivo e di partenza;
- le notti vendute contate nella griglia devono coincidere, mese per mese, con le
  *room nights* della tabella statistiche;
- i totali dichiarati dal portale devono tornare con la somma dei mesi.

L'elenco prenotazioni arricchisce il calendario (canale, ospiti, importi) ma non lo
condiziona: se non si legge, il calendario viene pubblicato comunque e il job segnala
il problema.

## Quando qualcosa non va

**La pagina avvisa da sola.**

| Avviso | Significato |
|---|---|
| «Dati non aggiornati» (tutta la pagina) | l'ultimo aggiornamento ha più di 36 ore: il job notturno non sta girando o fallisce |
| «*Casa*: dati non aggiornati» | il portale di quella casa non ha risposto; la casa mostra gli ultimi dati validi, l'altra è aggiornata |
| «*Casa*: dati non disponibili» | il portale non è stato letto e non esistono dati precedenti: la casa non compare, per non farla sembrare libera |

**GitHub manda una mail quando il job fallisce** (verificare in
`Settings → Notifications → Actions` del proprio account). L'errore è nel log del job:

| Messaggio | Causa probabile | Rimedio |
|---|---|---|
| `secret mancanti: …` | secret non inserito o con nome diverso | completare il passo 1 |
| `LoginError: credenziali rifiutate` | password del portale cambiata | aggiornare il secret |
| `TransportError: … HTTP 5xx` / `nessuna risposta` | portale irraggiungibile | di norma si risolve al run successivo |
| `LayoutError: …` | il gestore o Krossbooking hanno cambiato la pagina | va aggiornato il parser (`casecal/parse.py`) |
| `l'account contiene N appartamenti` | più unità nello stesso account | aggiungere il secret `<CASA>_KB_UNIT` con il nome dell'appartamento |
| `Get Pages site failed` | GitHub Pages non attivo | completare il passo 2 |

**Schedule disattivato.** GitHub spegne i workflow schedulati dei repository pubblici
dopo 60 giorni senza attività. Il job si riabilita da solo a ogni esecuzione; se
dovesse comunque fermarsi, `Actions → Aggiornamento notturno → Enable workflow`.

## Sviluppo

```bash
python -m pip install -r requirements-dev.txt
```

```bash
python -m pytest
```

```bash
node --test tests/js/*.test.js
```

Pagina di prova con dati inventati (le password vengono stampate):

```bash
python -m casecal demo --out dist-demo
```

Prova end-to-end in un browser vero, richiede Playwright con Chromium:

```bash
node tests/e2e/smoke.js dist-demo
```

Altri comandi: `python -m casecal check` verifica configurazione e secret senza
toccare la rete; `python -m casecal sync --out dist` esegue la lettura reale (servono
le variabili d'ambiente del passo 1).

| File | Ruolo |
|---|---|
| `properties.toml` | elenco delle case (pubblico: solo nomi) |
| `casecal/portal.py` | login, lettura delle pagine, logout |
| `casecal/parse.py` | HTML del portale → dati, con i controlli di coerenza |
| `casecal/model.py` | modello dati e formato pubblicato |
| `casecal/crypto.py` | derivazione delle chiavi e cifratura a due livelli |
| `casecal/sync.py` | orchestrazione notturna e recupero dell'ultimo stato valido |
| `casecal/site.py` | assemblaggio della pagina unica e della sua CSP |
| `casecal/web/` | la pagina: decifratura, logica del calendario, interfaccia |
| `tests/portal_html.py` | pagine sintetiche con la stessa struttura del portale |

Questo repository è pubblico: nei test non devono mai entrare pagine scaricate dai
portali, nomi, indirizzi o importi veri.

### Aggiungere una casa

1. Un nuovo blocco `[[property]]` in `properties.toml` (`id`, `label`, `env`; facoltativi
   `check_in` e `check_out` se gli orari differiscono da quelli generali).
2. I tre secret `<ENV>_KB_URL`, `<ENV>_KB_USER`, `<ENV>_KB_PASS`.
3. Le stesse tre righe nel passo «Leggi i portali» di
   `.github/workflows/nightly.yml` (un test verifica che non manchino).
