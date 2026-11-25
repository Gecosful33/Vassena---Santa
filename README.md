# Calendario case

Calendario delle case in affitto breve: una sola pagina per tutte, alimentata ogni
notte dai portali proprietari dei gestori (Krossbooking) e pubblicata cifrata.

- **Una sola password** apre la pagina. Chi ce l'ha vede, una casa alla volta, quando
  arrivano i prossimi ospiti, il calendario con le date occupate, bloccate e libere, e
  i conti: ricavi, trattenute e netto, mese per mese e in totale sull'anno.
- **Chiunque abbia la password** può segnare dalla pagina chi usa una casa e quando, e
  correggere o togliere quanto già segnato: lo vedono subito tutti, e per ogni periodo
  la pagina dice se quelle date sono davvero bloccate sul portale del gestore.
- **Il tasto «P&L»** (facoltativo) mette i ricavi netti letti dai portali a confronto con
  le spese annue di ciascuna casa, tenute in un foglio Excel privato: margine, incidenza
  delle trattenute, notti di pareggio.

La pagina è raggiungibile da chiunque, ma contiene solo testo cifrato: senza password
non si legge nulla, nemmeno i nomi delle case.

## Come funziona

```
ogni notte (GitHub Actions, 02:17 UTC)
  └─ per ogni casa: login al portale → statistiche dell'anno in corso e del successivo
     → elenco prenotazioni → logout
  └─ controlli di coerenza (vedi sotto)
  └─ rilettura delle voci inserite dalla famiglia (registro cifrato, vedi sotto)
  └─ lettura del foglio delle spese e delle foto delle case da un bucket privato
     (facoltativi: il P&L e le foto sulle schede)
  └─ cifratura → una sola pagina HTML → GitHub Pages

durante il giorno (nel browser di chi apre la pagina)
  └─ lettura delle voci inserite dopo l'ultimo aggiornamento notturno
  └─ ogni inserimento, modifica o eliminazione → una riga cifrata nel registro
```

È uno script deterministico, non un agente: nessun modello di linguaggio gira di
notte. Nel repository non viene scritto alcun dato: la pagina è un artefatto di deploy
e il registro di famiglia sta nei commenti di una issue.

Sui portali dei gestori il programma **legge soltanto**. Segnare un periodo nella
pagina non chiude nessuna data: è un promemoria condiviso.

## Messa in funzione

Da fare una volta sola, nell'ordine.

### 1. Inserire i 7 secret

`Settings → Secrets and variables → Actions → New repository secret`, uno per riga:

| Nome | Valore |
|---|---|
| `SANTA_KB_URL` | indirizzo del portale del gestore, es. `https://vr.krossbooking.com/<gestore>` |
| `SANTA_KB_USER` | utente del portale |
| `SANTA_KB_PASS` | password del portale |
| `VASSENA_KB_URL` | come sopra, per la seconda casa |
| `VASSENA_KB_USER` | utente del portale |
| `VASSENA_KB_PASS` | password del portale |
| `FAMILY_PASSPHRASE` | la password della pagina, una sola per tutti, almeno 16 caratteri |

Va bene incollare l'indirizzo della pagina su cui si atterra dopo il login
(`…/<gestore>/property`): conta solo la parte fino al nome del gestore.

La password protegge un testo cifrato che chiunque può scaricare e attaccare con
calma: la lunghezza è l'unica difesa. Quattro o cinque parole a caso separate da un
trattino vanno bene; `python -m casecal passphrase` ne genera una. Il programma rifiuta
password corte, ripetitive o uguali a quelle dei portali.

In origine i conti avevano una password propria, nel secret `OWNER_PASSPHRASE`. Non
serve più: il job la usa soltanto, se c'è ancora, per rileggere una pagina pubblicata
quando le password erano due. Dopo la prima esecuzione riuscita il secret si può
eliminare.

### 2. Attivare GitHub Pages

`Settings → Pages → Build and deployment → Source: GitHub Actions`.

### 3. Portare il codice sul ramo `main`

I workflow schedulati partono solo dal ramo predefinito.

### 4. Primo avvio

`Actions → Aggiornamento notturno → Run workflow`. Al termine, l'indirizzo della pagina
compare nel riepilogo del job (`https://<utente>.github.io/<repository>/`).

### 5. Condividere

Indirizzo e password a chi deve vedere il calendario. Con la stessa password si vedono
anche i conti: non c'è un accesso separato.

### 6. Attivare «Chi usa la casa» (facoltativo)

Senza questo passo la pagina funziona in sola lettura. Per permettere alla famiglia di
segnare i propri periodi serve un token GitHub che la pagina userà per scrivere nel
registro. Deve poter fare **una cosa sola**, perché arriva a chiunque abbia la password
di famiglia.

1. Sul proprio account GitHub: foto del profilo → `Settings → Developer settings →
   Personal access tokens → Fine-grained tokens → Generate new token`.
2. **Token name**: a piacere, es. `calendario-case-registro`.
3. **Expiration**: una scadenza a scelta (al massimo un anno) oppure nessuna. Alla
   scadenza le modifiche smettono di funzionare, la pagina lo dice e il job notturno
   lo segnala; basta ripetere questi passi con un token nuovo.
4. **Repository access**: `Only select repositories`, e nell'elenco **solo questo
   repository**.
5. **Permissions → Repository permissions → Issues**: `Read and write`. Nient'altro
   (`Metadata: Read-only` viene aggiunto da GitHub).
6. `Generate token` e copiare il valore: inizia con `github_pat_`.
7. Nel repository: `Settings → Secrets and variables → Actions → New repository secret`,
   nome `FAMILY_LOG_TOKEN`, valore il token.
8. `Actions → Aggiornamento notturno → Run workflow`. Alla prima esecuzione il job crea
   la issue «Registro di famiglia (dati cifrati: non modificare)» e la blocca; da quel
   momento nella pagina compare il pulsante «Aggiungi un periodo».

Le Issues devono essere attive nel repository (`Settings → General → Features →
Issues`, lo sono per impostazione predefinita). Un token classico (`ghp_…`) viene
rifiutato: non si può limitare a un solo repository.

**Le famiglie da proporre nel modulo.** Un altro secret facoltativo, `FAMILY_GROUPS`,
elenca le famiglie che usano le case, separate da punto e virgola (per esempio
`Famiglia Rossi; Famiglia Bianchi`). Nel modulo diventano scelte fisse, con «Altro» per
chiunque altro; senza questo secret il nome si scrive a mano. Sta in un secret e non in
`properties.toml` perché il repository è pubblico: i nomi viaggiano solo nella parte
cifrata della pagina.

### 7. Attivare il P&L (facoltativo)

Il P&L mette a confronto le entrate lette dai portali con le spese annue di ciascuna
casa. Le spese stanno in un foglio Excel che deve restare privato, e questo repository
è pubblico: il file sta quindi in un bucket privato di Cloudflare R2, da cui il job
notturno lo scarica con una chiave di sola lettura.

**Il file.** Una cartella di lavoro `.xlsx` con un foglio per anno, che ha per nome
l'anno (`2026`). In ogni foglio:

- una riga di intestazione che inizia con `Voce` e nomina le case, una per colonna,
  fino alla colonna `Totale`. Una colonna è di una casa se la sua intestazione inizia
  con il nome che la casa ha in `properties.toml`, o viceversa (`Santa Margherita`
  vale per `Santa`);
- una riga `SPESE` e, sotto, una spesa per riga: il nome, l'importo annuo in euro per
  ciascuna casa (`n.a.` o cella vuota dove la spesa non si applica) e, a destra del
  totale, una nota facoltativa;
- una riga il cui nome inizia con `Totale` (per esempio `Totale Spese`), che chiude
  l'elenco. Il totale scritto lì serve da controllo: se non coincide con la somma
  delle voci lette, il job lo segnala;
- facoltativa, una cella `Data Aggiornamento` con la data nella cella sotto: la pagina
  la riporta come ultima revisione.

Tutto il resto viene ignorato, comprese le righe delle entrate (quelle la pagina le
prende ogni notte dai portali) e i fogli con un altro nome. Il file va salvato da
Excel, o da un programma che salvi anche il risultato delle formule.

**Il bucket e la chiave.**

1. Su [dash.cloudflare.com](https://dash.cloudflare.com), menu `R2 object storage`.
   Alla prima apertura R2 va attivato e chiede un metodo di pagamento; un file e una
   lettura per notte restano nella fascia gratuita (10 GB e 10 milioni di letture al
   mese).
2. `Create bucket`, con un nome a piacere (per esempio `case-private`). Un bucket nasce
   privato: non vanno attivati né l'accesso pubblico (`r2.dev`) né un dominio. Il nome
   va scritto in `properties.toml`, alla riga `bucket = "<nome>"`: non è un dato
   riservato, perché per leggere il bucket servono l'account e le chiavi.
3. Dentro il bucket, `Upload`, e caricare il file.
4. In `R2 object storage → Overview`, riquadro `Account Details`: `Manage` accanto ad
   `API Tokens`, poi `Create Account API token` (o `Create User API token`: per un
   account personale è lo stesso).
5. Permesso **`Object Read only`**, limitato a quel solo bucket (nella stessa schermata
   si sceglie a quali bucket si applica). La scadenza è a scelta: quando arriva, il job
   segnala che la chiave è rifiutata e basta crearne una nuova.
6. Creare il token e copiare **Access Key ID** e **Secret Access Key**: Cloudflare li
   mostra una volta sola.
7. Nel repository, `Settings → Secrets and variables → Actions`, tre secret:

   | Nome | Valore |
   |---|---|
   | `COSTS_R2_URL` | l'Account ID di Cloudflare, copiato così com'è |
   | `COSTS_R2_ACCESS_KEY_ID` | l'Access Key ID |
   | `COSTS_R2_SECRET_ACCESS_KEY` | la Secret Access Key |

   L'Account ID sono 32 caratteri tra cifre e lettere da `a` a `f`. Cloudflare lo
   mostra, con il pulsante per copiarlo, in `R2 object storage → Overview` nel
   riquadro `Account Details`; da qualunque pagina del pannello lo copia anche la
   ricerca (`Search`, poi `Copy account ID`). Il bucket è quello di `properties.toml`
   e il foglio è l'unico file `.xlsx` che contiene: non c'è nient'altro da scrivere.

   In `COSTS_R2_URL` vanno bene anche, a scelta:

   - l'indirizzo dell'API S3, `https://<account>.r2.cloudflarestorage.com`, da solo o
     seguito da `/<bucket>`: è quello che Cloudflare mostra accanto alle chiavi;
   - l'indirizzo della pagina del bucket nel pannello di Cloudflare, copiato dalla
     barra del browser (`https://dash.cloudflare.com/<account>/r2/default/buckets/<bucket>`).

   Al posto di `<account>` e `<bucket>` ci sono sempre i valori veri: una riga copiata
   da queste istruzioni con i segnaposto non è un indirizzo. Per un bucket creato con
   una giurisdizione (per esempio quella europea) serve una delle due forme con
   l'indirizzo, che la contengono (`.eu.` nell'host, `eu` al posto di `default`): il
   solo Account ID non la dice. Se nel bucket ci sono più file `.xlsx` bisogna dire
   quale, con l'indirizzo dell'API S3 completo di `/<bucket>/<file>`, il nome scritto
   com'è, spazi compresi.
8. `Actions → Aggiornamento notturno → Run workflow`. Nel log compare `spese: lette` e
   nella pagina, dopo le schede delle case, il tasto «P&L».

Per aggiornare le spese basta ricaricare il file nel bucket con lo stesso nome: la
pagina le prende alla lettura notturna successiva. Se una notte il file non si scarica
o non si legge, la pagina tiene le spese già pubblicate e il job lo segnala.

### 8. Le foto delle case (facoltativo)

Una foto per casa, per riconoscerla a colpo d'occhio: compare sulla scheda della casa,
al posto del quadratino colorato, e in testa alla sua vista. Le foto sono private come
il foglio delle spese e non entrano nel repository: stanno nello stesso bucket del
passo 7, accanto al foglio, e il job le legge con la stessa chiave. Non servono altri
secret.

1. Una foto per casa. La pagina la mostra in orizzontale (una fascia larga sul
   telefono, un riquadro 3:2 su schermo grande), ritagliata attorno al centro. Vanno
   bene JPEG, PNG e WebP fino a 20 MB; non il formato HEIC di iPhone.
2. Caricarle nel bucket, accanto al foglio, con il nome `<id della casa>.jpg`, dove
   l'id è quello di `properties.toml`: `santa.jpg` e `vassena.jpg`. Vanno bene anche le
   estensioni `.jpeg`, `.png` e `.webp`, e qualunque nome che inizi con l'id della
   casa, comunque scritto: `Santa.JPG`, `santa 2.jpg`, `santa-cucina.jpeg`. Se più
   file portano il nome della casa, vale quello con il nome esatto, poi il più
   recente. Una foto caricata con tutt'altro nome si indica in `properties.toml`, nel
   blocco della casa, con `photo = "<nome del file>"`.

   Il job legge l'elenco del bucket e scavalca i file che non può usare: uno vuoto
   (un caricamento dal telefono può lasciare il nome senza il contenuto) o uno che
   non è un'immagine leggibile. Per sostituire una foto riuscita male basta quindi
   caricarne un'altra con il nome della casa, senza cancellare la prima.
3. `Actions → Aggiornamento notturno → Run workflow`. Nel log compare
   `foto: lette (santa, vassena)`; per una casa di cui non trova il file scrive
   `foto: nessun file per …`, senza che sia un errore. Se nel bucket ci sono immagini
   che non portano il nome di nessuna casa, il log dice quante sono.

Una foto verticale, o comunque più alta del riquadro, viene tagliata in orizzontale
dal job. Di norma attorno alla metà; se il soggetto è più in alto o più in basso lo
dice `photo_focus` nel blocco della casa, in percentuale dall'alto (`photo_focus = 26`:
il soggetto è a un quarto dell'altezza).

Il job non pubblica il file così com'è. Ne ricava un'immagine piccola (lato lungo di
840 pixel, al più 64 KB) e senza i dati che la fotocamera scrive nel file (luogo, data,
dispositivo), e la mette nella parte cifrata della pagina. Una casa senza file resta
senza foto, e non è un errore; per togliere una foto basta cancellare il file dal
bucket. Se una notte una foto non si scarica o non è un'immagine, la pagina tiene
quella già pubblicata e il job lo segnala.

### 9. Il pannello di controllo (facoltativo)

In testa a ogni casa la pagina può mostrare un pannello con tre cose: se il **WiFi** è
online, se **riscaldamento e raffrescamento** sono accesi e, per la casa sul lago, quanto
è **alto il lago**. Cosa mostra ogni casa lo dice `properties.toml` (`wifi`, `climate`,
`lake`); gli account delle app stanno nei secret. Una fonte senza i suoi secret resta
«Non rilevato», e non è un errore.

Il pannello si aggiorna **ogni ora** con il workflow «Pannello di controllo»
(`.github/workflows/status.yml`), che ripubblica la pagina già online cambiando solo il
pannello: i portali dei gestori li legge, come prima, soltanto il job notturno.

**Il lago** non richiede nessun secret: il livello (altezza idrometrica, in centimetri
sopra lo zero di Malgrate) è un dato pubblico, letto da [laghi.net](https://laghi.net/Page/laghi)
con la stessa richiesta che fa la loro pagina. Il pannello mostra due dati: l'altezza e
la tendenza (con la variazione nelle ultime ore e il margine alla soglia). Sopra
`attention_cm` il pannello avvisa, sopra `alarm_cm` è allarme: in entrambi i casi il job
apre la segnalazione «Lago alto: controllare la barca», assegnata al proprietario, e
GitHub gli manda una mail; la segnalazione si chiude da sola quando il lago scende. Per
provare la lettura: `python -m casecal lake`.

**Il clima** si legge dai servizi delle app, in sola lettura (il programma non può
accendere né spegnere nulla):

| App | Secret | Note |
|---|---|---|
| Airzone Cloud | `AIRZONE_USER`, `AIRZONE_PASS` | email e password dell'app |
| myVAILLANT | `VAILLANT_USER`, `VAILLANT_PASS` | email e password dell'app; `VAILLANT_COUNTRY` solo se l'account non è italiano (es. `germany`) |
| Daikin Onecta | `DAIKIN_CLIENT_ID`, `DAIKIN_CLIENT_SECRET`, `DAIKIN_REFRESH_TOKEN` | vedi sotto |

Conviene creare nelle app un utente invitato, dedicato al calendario, invece di usare
l'account principale. Airzone e Vaillant non pubblicano un'interfaccia per questo uso:
il programma fa ciò che fa l'app, e un loro cambiamento può interrompere la lettura
finché il codice non viene adeguato (il pannello mostra «Non rilevato»).

Daikin ha un'interfaccia ufficiale, che richiede un'autorizzazione fatta una volta a mano:

1. su <https://developer.cloud.daikineurope.com> entra **con lo stesso account dell'app
   Onecta**, crea un'applicazione e indica un Redirect URI `https://` qualsiasi che
   controlli (non `localhost`); annota Client ID e Client Secret;
2. sul tuo computer: `DAIKIN_CLIENT_ID=… DAIKIN_CLIENT_SECRET=… python -m casecal daikin-login --redirect-uri https://…`
   e segui le istruzioni (apri l'indirizzo, acconsenti, incolla l'indirizzo a cui torni);
3. salva i tre valori nei secret.

L'autorizzazione di Daikin si rinnova a ogni lettura e quella precedente smette di
valere: il job conserva l'ultima, cifrata con la password della pagina, nel testo della
issue «Stato del pannello di controllo (dati cifrati: non modificare)». Se la catena si
interrompe (issue cancellata, password della pagina cambiata, un anno senza letture) il
job lo dice nei log e va rifatto il punto 2. Daikin concede 200 richieste al giorno: una
lettura all'ora ne usa 24.

**Il WiFi** non ha una fonte propria (né il router con SIM né Eolo espongono uno stato
leggibile da fuori): è «Online» se almeno un'unità del clima della casa è vista dal suo
servizio, «Offline» se nessuna lo è (WiFi o corrente assenti), «Non rilevato» se nessuna
app ha risposto.

## Cosa mostra la pagina

Con la password si vede tutto. In alto c'è una scheda per casa, con la sua foto se è
stata caricata (passo 8): la pagina mostra una casa alla volta e ogni dispositivo
ricorda l'ultima scelta.

| Sezione | Contenuto, per la casa scelta |
|---|---|
| Pannello di controllo | WiFi, riscaldamento/raffrescamento e, sul lago, livello e tendenza (passo 9) |
| Prossimi ospiti | chi c'è ora e i prossimi arrivi |
| Calendario | soggiorni degli ospiti, date bloccate e periodi della famiglia |
| «Chi usa la casa» | periodi della famiglia, inseribili e modificabili da tutti |
| Area proprietà | date che la famiglia conta di usare ma ancora in vendita, con il link al portale; riquadri di sintesi dell'anno, grafico del netto, statistiche mensili |

Se il P&L è attivo (passo 7), dopo le schede delle case c'è il tasto «P&L»: riguarda
tutte le case insieme, per l'anno scelto.

| Parte | Contenuto |
|---|---|
| Riquadri | ricavi netti, spese annue, margine, margine % e notti di pareggio dell'insieme delle case |
| Prospetto | una colonna per casa e una di totale: entrate (ADR, notti vendute, ricavi lordi, trattenute, ricavi netti), spese voce per voce, risultato |
| «Dove vanno le spese» | una barra per voce, dalla più pesante, divisa fra le case; toccandola, il dettaglio per casa e la nota scritta nel foglio |

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
nelle schede «Prossimi ospiti» e nelle tabelle. I colori seguono l'ordine delle case in
`properties.toml`: la prima è viola, la seconda arancione.

Gli orari si impostano in `properties.toml` con `check_in` e `check_out`: valgono per
tutte le case, e una casa con orari diversi li ridefinisce nel proprio blocco.

Tre stati, con lo stesso nome ovunque:

- **Ospiti**: c'è una prenotazione.
- **Bloccata**: date chiuse alla vendita dal proprietario o dal gestore, senza ospiti.
- **Libera**: nessuna prenotazione e nessun blocco *all'ultimo aggiornamento*. Resta in
  vendita: può essere prenotata in qualsiasi momento. Prima di organizzarsi vanno fatte
  bloccare le date.

I nomi degli ospiti e le nazionalità non vengono mai letti dal portale: il programma
estrae solo date, canale, numero di ospiti e colonne di importi. Gli importi delle
singole prenotazioni non lasciano il job: nella pagina i conti sono per mese e per anno.

**Soggiorni propri registrati come prenotazioni.** Alcuni gestori inseriscono i
soggiorni dei proprietari come prenotazioni a importo zero invece che come date
bloccate. Per le case che lo dichiarano in `properties.toml` con
`unpaid_is_own_use = true`, una prenotazione con tutti gli importi a zero è trattata
come date bloccate: non compare tra i prossimi ospiti e un periodo di famiglia su
quelle notti risulta bloccato. Conta solo una riga di zeri espliciti, e se sulle stesse
date il portale elenca anche una riga con un importo resta una prenotazione: scambiare
degli ospiti per una casa libera è l'errore che costa. Le statistiche dell'area
proprietà restano quelle del portale, che quelle notti le conta come vendute.

### Chi usa la casa

Un periodo si aggiunge con «Aggiungi un periodo» oppure toccando un giorno del
calendario: casa, check-in, check-out, chi la usa (una delle famiglie in elenco, o
«Altro»), una nota facoltativa e il nome di chi scrive. Il dispositivo ricorda il nome
e la famiglia scelta l'ultima volta. Chiunque può poi modificare o eliminare un
periodo; l'eliminazione chiede conferma. Non ci sono utenti né permessi: vale la
fiducia fra chi ha la password, e il nome di chi scrive è quello che ciascuno dichiara.

Il periodo compare nell'elenco e nel calendario, come barra sopra quelle del gestore,
con il suo stato rispetto al portale:

| Stato | Significato |
|---|---|
| **Bloccata sul portale** | tutte le notti sono chiuse alla vendita: nessun ospite può prenotarle |
| **Da far bloccare al gestore** | alcune notti sono ancora in vendita (sono elencate): finché il gestore non le blocca, un ospite può prenotarle |
| **Ci sono ospiti in quelle date** | dopo l'inserimento è arrivata una prenotazione: il periodo va spostato |
| **Non verificabile sul portale** | il portale non pubblica ancora quelle date, o quella notte non è stato letto |

Lo stato si aggiorna da solo a ogni lettura notturna dei portali. Bloccare le date è
compito del gestore, su richiesta del proprietario: la pagina non scrive nulla sui
portali e non manda richieste a nessuno. Nell'area proprietà di ciascuna casa l'elenco
«Da far bloccare al gestore» raccoglie le notti ancora scoperte, con un link al portale
per controllare.

In «Chi usa la casa», accanto ad «Aggiungi un periodo», quando la casa ha periodi ancora
da bloccare compaiono «Controlla sul portale» e «Chiedi il blocco su WhatsApp», che apre WhatsApp
con la richiesta già scritta (i periodi di quella casa ancora da bloccare, dal giorno di
arrivo a quello di partenza) e fa scegliere la chat o il gruppo a cui mandarla. La
pagina non conosce numeri e non invia nulla da sé: il messaggio parte solo quando lo si
invia da WhatsApp.

**Più famiglie nella stessa casa.** Due periodi possono condividere delle notti nella
stessa casa: il modulo lo dice prima di salvare («In quelle date c'è anche…») e nel
calendario le notti in comune sono una sola barra con entrambi i nomi. La pagina
rifiuta invece un periodo che cade su notti con ospiti o più lungo di 366 notti;
arrivare il giorno in cui qualcuno parte è ammesso, come per gli ospiti. Se due persone
modificano lo stesso periodo negli stessi istanti vale l'ultima scrittura.

**Avviso al proprietario.** Per non dover aprire la pagina per accorgersene, il job
notturno tiene aperta una issue «Date da far bloccare al gestore», assegnata al
proprietario del repository: GitHub lo notifica, per mail con le impostazioni
predefinite dell'account. Il job parte anche da solo, entro un paio di minuti, ogni
volta che qualcuno inserisce un periodo: la mail non aspetta la notte. Succede quando un periodo
entra in «Da far bloccare al gestore» o in «Ci sono ospiti in quelle date» (appena
inserito, perché il gestore ha riaperto la vendita, o perché è arrivata una
prenotazione); il lunedì arriva un promemoria finché restano periodi non bloccati che
iniziano entro 90 giorni. Quando non resta nulla in sospeso il job chiude la issue. Il
testo è generico, senza date né nomi, perché la issue è pubblica: il dettaglio è nella
pagina. La issue la scrive GitHub Actions e non il proprietario, perché nessuno riceve
notifiche di ciò che fa da sé. I periodi di una casa che quella notte non è stata
letta, o è stata letta con avvisi, non vengono giudicati: la segnalazione resta com'è
(e il job risulta comunque fallito); l'elenco nella pagina resta il riferimento.

Le modifiche degli altri arrivano quando si riapre la pagina o si torna sulla scheda
(e comunque entro un minuto e mezzo); il job notturno le consolida nella pagina.

Gli importi sono quelli dei portali, con le colonne che ciascun gestore ha
configurato (possono differire da casa a casa). Le uniche grandezze calcolate sono
marcate con un asterisco: lordo = notti × ADR, e il rapporto netto su lordo.

Nella tabella delle statistiche mensili gli importi sono in euro interi, senza simbolo
(lo dice la riga sotto la tabella): le colonne sono molte e su un telefono ogni
carattere in meno è scorrimento laterale in meno. Ogni cifra è arrotondata per conto
proprio, quindi il totale dell'anno può differire di poco dalla somma dei mesi; i
riquadri sopra il grafico restano al centesimo.

I conti sono per mese e per anno. Le singole prenotazioni non sono elencate né
sommate, e i loro importi non vengono pubblicati: il job legge l'elenco del portale
solo per dare a ogni soggiorno il canale e il numero di ospiti, e per riconoscere le
prenotazioni a importo zero.

### Come si legge il P&L

L'anno si sceglie dal menù a tendina accanto al titolo: ci sono tutti gli anni che il
job legge per intero dai portali: quello in corso, i successivi secondo `years_ahead` e
quelli già chiusi secondo `years_back`, mai prima di `first_year` (in `properties.toml`).
Le entrate sono sempre quelle dell'anno scelto. Le spese sono quelle del foglio di
quell'anno; se il file non ha ancora un foglio per quell'anno valgono quelle del foglio
più recente fra i precedenti, e la pagina lo dice («foglio delle spese del 2026, in
attesa del foglio 2027»). Per dare a un anno spese proprie basta aggiungere al file un
foglio con quell'anno per nome.

- **Entrate**: quelle dei portali per l'anno, le stesse dell'area proprietà. Per l'anno
  in corso sono le prenotazioni acquisite finora, quindi crescono durante l'anno; le
  spese sono invece gli importi dell'anno intero.
- **Ricavi lordi** = notti × ADR. **Trattenute** = lordi − netti, cioè quanto resta a
  gestore, canali e fisco, con la sua incidenza sul lordo. **Ricavi netti** = il netto
  proprietario dei portali.
- **Margine** = ricavi netti − spese. **Margine %** = margine sui ricavi netti.
- **Notti di pareggio** = le notti che servono a coprire le spese, al netto medio per
  notte dell'anno (spese ÷ netto per notte, arrotondato per eccesso). Accanto ci sono
  le notti vendute.
- Gli importi sono arrotondati all'euro, ciascuno per conto proprio: un totale può
  differire di 1 € dalla somma delle sue voci. Restano al centesimo l'ADR e il
  dettaglio che compare toccando una barra.
- Il P&L si calcola per gli anni che il job legge dai portali, cioè quello in corso e
  il successivo: da gennaio il foglio dell'anno appena chiuso non ha più entrate con
  cui confrontarsi, e la pagina dice quale foglio manca. Una casa che quella notte non
  è stata letta resta fuori dal conto, e la pagina lo dice.
- Notti e ADR sono quelli delle statistiche del portale. Dove il gestore registra i
  soggiorni dei proprietari come prenotazioni a importo zero, quelle notti contano come
  vendute (vedi sopra): abbassano l'ADR e il netto per notte, e quindi alzano le notti
  di pareggio. Il margine non ne risente.
- Le note scritte nel foglio accanto alle spese compaiono nella pagina: le legge
  chiunque abbia la password.

## Sicurezza: cosa è protetto e cosa no

- **Cifratura.** Chiavi derivate con PBKDF2-HMAC-SHA256 (600.000 iterazioni), contenuti
  cifrati con AES-256-GCM. Il browser decifra in locale con WebCrypto; la password non
  lascia mai il dispositivo.
- **Un solo accesso.** Calendario e conti sono due blocchi cifrati con la stessa chiave:
  chi ha la password vede tutto. Non esiste un livello riservato alla proprietà.
- **Una sola connessione in uscita.** La pagina è un unico file con una
  Content-Security-Policy che ammette solo il proprio script e il proprio stile (per
  hash). Non carica nulla da nessun sito; l'unico indirizzo che può contattare è
  `api.github.com`, e lo fa solo se «Chi usa la casa» è attivo, per leggere e scrivere
  il registro di famiglia.
- **Registro di famiglia.** Ogni modifica è un commento della issue «Registro di
  famiglia», cifrato nel browser con la chiave di famiglia (AES-256-GCM) e riempito
  fino a una lunghezza standard, perché la lunghezza non riveli nulla. La issue è
  pubblica come il repository: chiunque può vedere *quante* modifiche ci sono e *quando*
  sono state fatte, non che cosa dicono. È bloccata: può aggiungervi commenti solo chi
  ha accesso in scrittura al repository, cioè il proprietario e il token a suo nome.
- **«Ricorda su questo dispositivo»** salva nel browser la chiave derivata, non la
  password. «Blocca» la cancella. Non usarlo su dispositivi condivisi.
- **Log pubblici.** Il repository è pubblico, quindi lo sono i log di Actions: il
  programma stampa solo l'esito per casa e il tipo di errore.
- **Segnalazione al proprietario.** La issue «Date da far bloccare al gestore» è
  pubblica e dice solo che c'è qualcosa da guardare. Il job la scrive con il token del
  workflow, a cui per questo è concesso il permesso `issues: write`.
- **Foto delle case.** Stanno nello stesso bucket privato. Il job ne pubblica una copia
  ridotta e senza metadati dentro la parte cifrata della pagina: senza password non si
  vedono, chi ha la password le vede.
- **Foglio delle spese.** Il file sta in un bucket privato, fuori dal repository. Il job
  lo scarica con una chiave che può soltanto leggere quel bucket; nella pagina arrivano,
  cifrati insieme ai conti, solo i nomi delle spese, gli importi e le note. Il browser
  non contatta mai Cloudflare. Nei log un errore indica al massimo il foglio e il numero
  di riga, mai un nome o un importo.

Limiti da conoscere:

- Chi ha la password vede calendario e conti finché la password non cambia. Per
  revocare un accesso bisogna cambiare `FAMILY_PASSPHRASE` e ridistribuirla; al run
  successivo le chiavi ricordate sui dispositivi smettono di funzionare.
- Una password debole si indovina offline. Non riutilizzare password esistenti.
- Le credenziali dei portali stanno nei secret di GitHub: chi ha accesso in scrittura
  al repository può farle uscire modificando un workflow. Tenere il repository
  personale e l'account GitHub protetto da autenticazione a due fattori.
- La chiave di R2 sta nei secret come le credenziali dei portali, con gli stessi limiti.
  Può solo leggere il bucket indicato: se trapela espone il file delle spese e
  nient'altro. Si revoca da Cloudflare (`API Tokens`) e se ne crea una nuova.
- Il token del registro viaggia dentro la pagina cifrata: chi ha la password può
  estrarlo. Con il solo permesso Issues su questo
  repository può creare, modificare e cancellare issue e commenti a nome del
  proprietario dell'account (quindi anche rovinare il registro), ma non toccare codice,
  workflow, secret o la pagina. Per questo il permesso non va mai allargato.
- Cambiare la password non revoca il token. Per escludere qualcuno vanno
  rifatti entrambi: token nuovo (quello vecchio si elimina da `Developer settings`) e
  password nuova.
- Dopo un cambio di `FAMILY_PASSPHRASE` le voci già scritte restano cifrate con la
  chiave precedente. Per portarle sotto la nuova, aggiungere per una sola esecuzione il
  secret `OLD_FAMILY_PASSPHRASE` con la password vecchia, poi cancellarlo.
- La chiave dipende anche dal nome del repository. Prima di rinominarlo, fissare in
  `properties.toml` la riga `site_id = "<utente>/<nome attuale>"`, altrimenti le voci
  del registro non si aprono più.
- La issue del registro non va rinominata, chiusa o cancellata, e i suoi commenti non
  vanno toccati: sono l'unica copia delle voci inserite.

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

L'elenco prenotazioni arricchisce il calendario (canale e numero di ospiti) ma non lo
condiziona: se non si legge, il calendario viene pubblicato comunque e il job segnala
il problema.

Lo stesso vale per il foglio delle spese, che non condiziona mai il calendario. Un
importo che non è un numero, una formula senza risultato, un'intestazione che
corrisponde a due case o un elenco senza la riga di chiusura fanno scartare il file:
la pagina tiene le spese già pubblicate e il job lo segnala. Un totale scritto nel
foglio che non coincide con la somma delle voci viene segnalato, e la pagina usa la
somma delle voci.

## Quando qualcosa non va

**La pagina avvisa da sola.**

| Avviso | Significato |
|---|---|
| «Dati non aggiornati» (tutta la pagina) | l'ultimo aggiornamento ha più di 36 ore: il job notturno non sta girando o fallisce |
| «*Casa*: dati non aggiornati» | il portale di quella casa non ha risposto; la casa mostra gli ultimi dati validi, l'altra è aggiornata |
| «*Casa*: dati non disponibili» | il portale non è stato letto e non esistono dati precedenti: la casa non compare, per non farla sembrare libera |
| «Modifiche recenti non lette» (in «Chi usa la casa») | il registro di famiglia non risponde: la riga accanto dice se manca la connessione, se il token è scaduto o revocato, o se GitHub sta limitando le richieste. Le voci già note restano visibili |

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
| `registro di famiglia non letto (… token rifiutato …)` | token scaduto o eliminato | ripetere il passo 6 con un token nuovo |
| `… il token non ha il permesso Issues su questo repository` | token creato per un altro repository o senza permesso | ripetere il passo 6 |
| `… le Issues sono disattivate in questo repository` | funzione Issues spenta | `Settings → General → Features → Issues` |
| `FAMILY_LOG_TOKEN non è un token fine-grained` | inserito un token classico | ripetere il passo 6: il token deve iniziare con `github_pat_` |
| `N voci del registro di famiglia non si aprono con la password attuale` | password cambiata | impostare `OLD_FAMILY_PASSPHRASE` per un'esecuzione |
| `FAMILY_GROUPS: …` | un nome ripetuto, troppo lungo, o più di 8 nomi | correggere il secret: la pagina usa intanto i nomi validi |
| `avviso al proprietario non aggiornato (…)` | GitHub ha rifiutato la scrittura della segnalazione | controllare che il workflow abbia ancora `issues: write`; l'elenco nella pagina resta corretto |
| `spese non lette (COSTS_R2_URL non valido: …)` | il valore del secret non dice dov'è il bucket; il messaggio dice perché: è rimasto un segnaposto come `<ACCOUNT_ID>`, l'Account ID non ha 32 caratteri, c'è una chiave al posto dell'indirizzo, è l'indirizzo pubblico `r2.dev`… | incollare nel secret l'Account ID, come al punto 7 del passo 7; nessuna richiesta è partita, quindi le chiavi non sono ancora state provate |
| `spese non lette (firma rifiutata: …)` | la Secret Access Key non è quella dell'Access Key ID: incollata male, oppure è il `Token value` | creare un nuovo token (punti 4–6 del passo 7) e ricopiare le due chiavi |
| `spese non lette (Access Key ID non riconosciuto …)` | Access Key ID incollato male, oppure token eliminato | come sopra |
| `spese non lette (accesso negato: …)` | la chiave è valida ma non può leggere quel bucket: token limitato a un altro bucket o senza permesso di lettura, oppure nome del bucket sbagliato in `properties.toml` | correggere il nome del bucket, o creare un token `Object Read only` limitato a quel bucket |
| `spese non lette (chiave rifiutata, …)` | chiave di R2 sbagliata, scaduta o non valida per quel bucket | ripetere i punti 4–7 del passo 7 |
| `… Nota: COSTS_R2_… non ha la forma di …` | aggiunta a un rifiuto quando una delle due chiavi non ha la lunghezza di una chiave di R2 (32 e 64 caratteri esadecimali), o le due sembrano scambiate | ricopiare le chiavi nei secret giusti |
| `spese non lette (file non trovato: …)` | file con un nome diverso da quello in `COSTS_R2_URL`, anche solo per una maiuscola | correggere il secret, o ricaricare il file con il nome atteso |
| `spese non lette (bucket non trovato: …)` | nome del bucket sbagliato, oppure bucket con giurisdizione europea raggiunto senza `.eu.` nell'host | correggere `bucket` in `properties.toml`; per un bucket con giurisdizione, mettere in `COSTS_R2_URL` l'indirizzo della sua pagina nel pannello |
| `spese non lette (nel bucket non c'è nessun file Excel (.xlsx))` | il foglio non è stato caricato, o è un `.xls` | caricare il foglio in formato `.xlsx` |
| `spese non lette (nel bucket ci sono più file Excel (.xlsx): …)` | il job non sa quale sia il foglio delle spese | aggiungere a `COSTS_R2_URL` `/` e il nome del file, oppure togliere dal bucket quelli in più |
| `spese non lette (servono tutti e tre i secret …)` | manca uno dei tre secret | completare il passo 7 |
| `spese non lette (il file non è una cartella di lavoro Excel …)` | caricato un `.xls`, un `.csv` o un file danneggiato | salvare come `.xlsx` e ricaricare |
| `spese non lette (foglio 2026, riga 14: …)` | in una cella di importo c'è del testo, o una formula senza risultato | correggere quella riga (`n.a.` è ammesso) e ricaricare |
| `spese non lette (foglio 2026: non trovo …)` | manca la riga `Voce`, la riga `SPESE` o quella che inizia con `Totale` | ripristinare la struttura del passo 7 |
| `foglio 2026: il totale delle spese scritto nel foglio non è la somma delle voci` | la formula del totale non comprende tutte le righe | correggere la formula; la pagina usa comunque la somma delle voci |
| `foglio 2026: una colonna non corrisponde a nessuna casa` / `manca la colonna della casa …` | intestazione diversa dal nome della casa | rinominare la colonna |
| `foto non lette (il file è vuoto (0 byte): …): santa` | il caricamento nel bucket ha lasciato il nome ma non il contenuto: capita caricando dal telefono | ricaricarla come `santa.jpg`, meglio da un computer o da un altro browser, e controllare che nel bucket la dimensione non sia zero; il file vuoto può restare |
| `foto non lette (i 2 file con il nome della casa sono tutti vuoti …): santa` | come sopra, più volte: anche il nuovo caricamento è arrivato senza contenuto | caricare la foto per un'altra via (computer, altro browser) |
| `foto: nel bucket ci sono N immagini che non portano il nome di una casa` | una foto è stata caricata con un nome da cui il job non può capire di quale casa sia | rinominarla `<id della casa>.jpg`, o indicarla con `photo` in `properties.toml` |
| `foto non lette (il file è in formato HEIC, …): santa` | foto di iPhone caricata senza conversione, anche se il nome finisce per `.jpeg` | esportarla in JPEG e ricaricarla |
| `foto non lette (il file JPEG è danneggiato o incompleto (…)): santa` | trasferimento interrotto, o file rovinato | caricare di nuovo la foto |
| `foto non lette (il file non è un'immagine …): santa` | sotto il nome della foto c'è altro: il messaggio dice cosa (un documento, un video, un testo) e quanto è grande | caricare la foto in JPEG, PNG o WebP |
| `foto non lette (file troppo grande …)` / `(immagine troppo grande …)` | file oltre 20 MB, o oltre 60 megapixel | ridurre la foto e ricaricarla |
| `foto non lette (chiave rifiutata, …)` / `(Cloudflare R2 non raggiungibile …)` | la stessa causa di `spese non lette` | vedi le righe sopra |

Quando il registro non si legge, la pagina viene pubblicata lo stesso con le voci già
note e il job risulta fallito, così arriva la mail. Finché il token non funziona la
famiglia non può inserire né modificare periodi.

Anche un problema con il foglio delle spese o con una foto fa risultare il job fallito,
dopo aver pubblicato la pagina: il calendario è aggiornato, il P&L mostra le spese
dell'ultima lettura riuscita e le case tengono la foto che avevano.

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

Pagina di prova con dati inventati (la password viene stampata):

```bash
python -m casecal demo --out dist-demo
```

Nella pagina di prova le modifiche a «Chi usa la casa» restano nel browser che le fa.

Prova end-to-end in un browser vero, richiede Playwright con Chromium (la CI la esegue
a ogni push, con la versione fissata in `.github/workflows/ci.yml`). Simula anche due
dispositivi sullo stesso registro, con una finta API di GitHub, e rilegge quanto
scritto con il codice del job notturno:

```bash
node tests/e2e/smoke.js dist-demo
```

Altri comandi: `python -m casecal check` verifica configurazione e secret senza
toccare la rete; `python -m casecal sync --out dist` esegue la lettura reale (servono
le variabili d'ambiente del passo 1, e quelle del passo 7 per il P&L).

| File | Ruolo |
|---|---|
| `properties.toml` | elenco delle case (pubblico: solo nomi) |
| `casecal/portal.py` | login, lettura delle pagine, logout |
| `casecal/parse.py` | HTML del portale → dati, con i controlli di coerenza |
| `casecal/model.py` | modello dati e formato pubblicato |
| `casecal/crypto.py` | derivazione della chiave e cifratura della pagina |
| `casecal/family.py` | voci della famiglia: validazione, eventi cifrati, ricostruzione dello stato |
| `casecal/familylog.py` | registro di famiglia su GitHub: lettura notturna e impostazioni per la pagina |
| `casecal/alerts.py` | segnalazione al proprietario delle date da far bloccare |
| `casecal/costs.py` | foglio Excel delle spese → voci per casa, con i suoi controlli |
| `casecal/photos.py` | foto delle case: da un file qualsiasi a un'immagine piccola e senza metadati |
| `casecal/r2.py` | lettura dal bucket privato (un file, o l'elenco dei file) con richieste firmate, in sola lettura |
| `casecal/sync.py` | orchestrazione notturna e recupero dell'ultimo stato valido |
| `casecal/site.py` | assemblaggio della pagina unica e della sua CSP |
| `casecal/web/` | la pagina: decifratura, logica del calendario, registro (`store.js`), interfaccia |
| `tests/portal_html.py` | pagine sintetiche con la stessa struttura del portale |
| `tests/github_fake.py` | controfigura dell'API di GitHub usata dal registro e dalla segnalazione |
| `tests/workbooks.py` | fogli di spese inventati, nel formato che il job si aspetta |
| `tests/pictures.py` | immagini inventate, disegnate sul momento dai test |
| `tests/fixtures/entry_status.json` | casi su cui job notturno e pagina devono dare lo stesso stato a un periodo |

Questo repository è pubblico: nei test non devono mai entrare pagine scaricate dai
portali, nomi, indirizzi o importi veri, né il foglio delle spese o le sue cifre, né
le foto delle case.

### Aggiungere una casa

1. Un nuovo blocco `[[property]]` in `properties.toml` (`id`, `label`, `env`; facoltativi
   `check_in` e `check_out` se gli orari differiscono da quelli generali, e
   `unpaid_is_own_use = true` se il gestore registra i soggiorni propri come
   prenotazioni a importo zero).
2. I tre secret `<ENV>_KB_URL`, `<ENV>_KB_USER`, `<ENV>_KB_PASS`.
3. Le stesse tre righe nel passo «Leggi i portali» di
   `.github/workflows/nightly.yml` (un test verifica che non manchino).
