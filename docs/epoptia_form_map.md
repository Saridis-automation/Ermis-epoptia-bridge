# Epoptia — χάρτης endpoints & φορμών (ανάγνωση μόνο)

Έλεγχος: 2026-09-30, από `Ermis-server`, όλα μέσω `epoptia_throttle.call()`.
Κλήσεις: 5 × GET API, login (GET + POST `/login`, όπως κάνει ήδη το `epoptia_read.py`),
3 × GET σελίδων. Καμία υποβολή φόρμας, καμία εγγραφή. Κανένα 403/429 — δεν γράφτηκε halt.

## α) API `GET /api/3.03/<command>` (header `X-Auth-Token`)

| Endpoint | HTTP | Απάντηση |
|---|---|---|
| `/api/3.03/products` | 400 | `Command \`products\` for api version 3.03 not found` |
| `/api/3.03/clients` | 400 | `Command \`clients\` …not found` |
| `/api/3.03/workorders` | 400 | `Command \`workorders\` …not found` |
| `/api/3.03/workflows` | 400 | `Command \`workflows\` …not found` |
| `/api/3.03/workstations` | 400 | `Command \`workstations\` …not found` |

Συμπέρασμα: το key γίνεται δεκτό (όχι 401/403). Το API δουλεύει με ονόματα «commands»
και αυτά τα πέντε δεν υπάρχουν στο v3.03 με αυτά τα ονόματα. Το γνωστό που δουλεύει είναι το
`workorderlines`. Τα σωστά ονόματα commands μένουν να βρεθούν.

## β) Web UI (session login + CSRF)

Όλες οι σελίδες είναι server-rendered Laravel/Blade + jQuery (όχι Vue). CSRF: hidden `_token`
στις φόρμες, ή `meta[name=csrf-token]` για AJAX.

### `/product/create` → 200

**Δημιουργία προϊόντος** — `<form id="productCreateForm" method="POST" action="/products/store">`
(κλασικό form submit, όχι AJAX)

| Πεδίο | Τύπος | Υποχρ. | Σημείωση |
|---|---|---|---|
| `_token` | hidden | ναι | CSRF |
| `product_type` | radio | — | `Product` (προεπιλογή)· `Recipe`/`Service`/`Material` disabled |
| `name` | text | **ναι** | jQuery validate: max 150 χαρακτήρες |
| `is_active` | checkbox | — | προεπιλογή checked |
| `infosupplier[]` | text | — | προμηθευτής |
| `infoprice[]` | number | — | τιμή |
| (χωρίς name) `imageUploadMaterial` | file | — | εικόνα, χειρίζεται με JS |

Η φόρμα δημιουργίας **δεν** έχει ροή εργασίας (workflow) ή κωδικό — ορίζονται μετά.

**Επεξεργασία προϊόντος** — `form#productForm POST /products/update/{id}`, `_method=PUT`

| Πεδίο | Τύπος | Σημείωση |
|---|---|---|
| `_token`, `_method=PUT` | hidden | |
| `name` | text | |
| `comments` | text | |
| `workflows` | text | |
| `remote_id`, `remote_code` | text | κωδικοί ERP |
| `scomment` | text | |
| `active` | text | |
| `total_average` | text | |

**Μαζική ενημέρωση** — `form#editModels POST /products/update-all`: `workflow` (select, 23 επιλογές),
`tag_function` (select, 5), `product_tag[]` (select), `model_ids` (hidden, λίστα ids).

**Διαγραφή** (ΜΗΝ αγγιχτεί): `POST /products/destroy/{id}` (`_method=DELETE`), `POST /products/delete-all`.

### `/workorders/create` → 200

Οι φόρμες είναι `method=GET` χωρίς action. Το JS κάνει `preventDefault` και στέλνει AJAX
(`POST`, body JSON με `_token` μέσα). Δύο διαδοχικές κλήσεις:

1. `POST /workorders/store` — επιστρέφει `{id}`

   | Πεδίο JSON | Από UI | Υποχρ. |
   |---|---|---|
   | `client` | `#clientSelect` (id επαφής· αναζήτηση `POST /search/client`) | **ναι** |
   | `productionDate` | `#productionDateSelect`, μορφή `DD-MM-YYYY`, ≥ σήμερα | **ναι** |
   | `workorderCode` | `#workorderCode` | — |
   | `comments` | `#workOrderComment` | — |

2. `POST /workorderlines/store` — `{workorderId, productionDate, workorderLines:[…]}`, κάθε γραμμή:

   | Πεδίο | Από UI | Υποχρ. |
   |---|---|---|
   | `product` | `#productSelect` (αναζήτηση `POST /search/product`) | **ναι** |
   | `description` | `#workorderLineDescription` | **ναι** |
   | `quantity` | `#quantity` (number) | **ναι** |
   | `tag` | `#tagSelect` (`POST /search/workorderline-tag`) | — |
   | `comments` | `#workorderLineComment` | — |
   | `wolCode` | `#wolCode` | — |
   | `frontId` | προσωρινό id του UI | — |
   | `customFieldsValues`, `bomValues` | αντικείμενα `{fieldId: value}` | — |

   Απάντηση: `workorderLines` = χάρτης `frontId → νέο WOL id`.

Η UI στέλνει μόνο αν υπάρχει ≥1 γραμμή. Αν αποτύχει η 2η κλήση, μένει εντολή χωρίς γραμμές.

Στην ίδια σελίδα: **νέα επαφή** `form#clientCreateForm POST /clients/store`: `tag` radio
(πελάτης/προμηθευτής/και τα δύο), `name` (text, **υποχρ.**), `email`, `city`, `comments`,
`show_comments` (checkbox), `phone_number`, `vat_number`, `vat_rate` (number).

Σημείωση: στο AJAX helper το `contentType` ορίζεται λανθασμένα ως string
`'contentType: "application/json"'` (bug του Epoptia). Το body πάντως είναι JSON.

### `/workflows` → 200

- **Νέα ροή**: `POST /workflows/store` — `_token`, `name` (text, **υποχρ.**). Δημιουργεί
  κενή ροή· τα βήματα/σταθμοί ορίζονται στη σελίδα `/workflows/{id}` (δεν διαβάστηκε ακόμα).
- **Διαγραφή** (ΜΗΝ αγγιχτεί): `POST /workflows/destroy/{id}`, `_method=DELETE`.
- Λίστα ροών: 9 στην 1η σελίδα (3 σελίδες), όλες `workflow_type=template`.
- Υπάρχουν επίσης σελίδες `/workflows/create` και `/workflows/{id}` (όχι ακόμα).

## Ανοιχτά

- Σωστά ονόματα commands του API v3.03 για products/clients/workorders (τα 400 δεν κάνουν halt).
- Σελίδα `/workflows/{id}` (επεξεργασία βημάτων) και `/workstations` — όχι ακόμα.

## Πρώτη εγγραφή (2026-09-30)

- `POST /products/store` (ERMIS-TEST, `is_active` off) → **302 → `/products`**. Δημιουργήθηκε **#1427**.
- Η απάντηση δεν δίνει το id. Τα **ανενεργά προϊόντα δεν εμφανίζονται** στη λίστα
  `/product/create` (ούτε με `term`, ούτε με `isactive=0`, που δείχνει τα διαγραμμένα).
  Επιβεβαίωση γίνεται με `GET /products/{id}`: breadcrumb `#id (όνομα)`.
- `POST /clients/store` (ERMIS-TEST, tag=Client) → **302 → `/clients`**. Δημιουργήθηκε **#387**
  (φαίνεται στη λίστα `/client/create?term=`).
- `POST /workorders/store` (JSON, client 387, 31-12-2026, κωδ. ERMIS-TEST-1) → **200 `{"id": 750}`**.
- `POST /workorderlines/store` (JSON, product 1427, qty 1) → **200 `{"workorderLines": {"1": 3287}}`**.
  Ανενεργό προϊόν χωρίς ροή εργασίας έγινε δεκτό σε γραμμή εντολής.
- Σειρά εγγραφών: επαφή → εντολή (με το id της επαφής) → γραμμές (με το id της εντολής).

Δοκιμαστικά στοιχεία (κρατιούνται για δοκιμές): προϊόν #1427, επαφή #387, εντολή #750, γραμμή #3287.

## Πλήρες προϊόν με ροή και ειδικά πεδία (2026-09-30)

Πώς είναι στημένα τα προϊόντα της SARIDIS (από υπάρχουσες βιτρίνες/ψυγεία):
- **Ροή εργασίας στο προϊόν**, από template: `POST /product/workflow/from-template`
  (`_token`, `sectionId`=product id, `templateId`=workflow id) → 302 → `/product/{id}/workflow/{tpl}`.
  Η σελίδα `/products/{id}` έχει `#templatePickerEmptyWorkflow` όταν δεν υπάρχει ροή. Αλλαγή
  υπάρχουσας ροής «χάνει τις ρυθμίσεις του προϊόντος» — το εργαλείο το αρνείται.
- **Η γραμμή εντολής αντιγράφει τη ροή του προϊόντος** κατά τη δημιουργία (`erp_routing`).
  Άρα σειρά: προϊόν → ροή → εντολή → γραμμές.
- **Ειδικά πεδία** έρχονται από τη ροή· τιμές μπαίνουν ανά γραμμή (`customFieldsValues`):
  6 ΠΑΧΟΣ ΛΑΜΑΡΙΝΑΣ, 7 ΠΟΙΟΤΗΤΑ, 8 ΜΗΧΑΝΗ, 9 ΣΤΟΙΧΕΙΟ, 10 ΦΡΕΟΝ, 11 ΦΩΤΙΣΜΟΣ, 12 ΔΙΑΣΤΑΣΕΙΣ.
- Σύμβαση: περιγραφή γραμμής = όνομα προϊόντος· σχόλια γραμμής = λίστα χαρακτηριστικών
  (1η γραμμή χωρίς κουκκίδα, μετά «• …»)· διαστάσεις π.χ. `180x85x120 cm`.

Αποτέλεσμα: προϊόν **#1428** «Ψυγείο βιτρίνα συντήρησης BXM83 Ειδικό» (ανενεργό) → ροή 39
«ΨΥΓΕΙΟ ΒΙΤΡΙΝΑ ΧΩΡΙΣ ΑΠΟΘΗΚΗ v.2» → εντολή **#751 ERMIS-TEST-2** (επαφή #387)
→ γραμμή **#3288** με 21 βήματα ροής και όλα τα 7 ειδικά πεδία (επιβεβαίωση από το API).

## Ροές εργασίας (workflows) — 2026-10-02

Εργαλείο: `epoptia_workflows.py` (`WorkflowWriter`: read / create / update / delete, preview χωρίς `confirm`).
- **Αποθηκευμένη ροή:** στη σελίδα `/workflows/{id}`, JSON στο `#workflowServer`.
  Περιέχει `elements` (βήμα: `workstation_id`, `tag`/`tag_id`, strict/semi/free, `is_or`, θέση,
  σχόλιο) και `links` (`parent_element` → `child_element`, `output_id`/`input_id`).
- **Αποθήκευση:** `POST /workflow-update/{id}`, `_method=put`, με ΟΛΟ το διάγραμμα.
  Το `workflowData` είναι `{lineId: {source, target, input, output, id}}`, όπου κόμβος = `se_id`, `id`, `node_id` (0 = νέο).
  Βήματα χωρίς σύνδεση δεν σώζονται. Κάθε βήμα θέλει ετικέτα.
  Τα υπάρχοντα βήματα και οι συνδέσεις κρατούν τα ids τους όταν στέλνονται με `node_id` / `id`.
- **Νέα ροή:** η σελίδα `/workflows/create` στέλνει `POST /workflow-create` (όνομα + διάγραμμα).
  Το modal `/workflows/store` απαντά 405 (νεκρή διαδρομή).
- **Διαγραφή:** `POST /workflows/destroy/{id}`, `_method=DELETE`, με απάντηση 302.
- **Ειδικά πεδία κολλημένα στη ροή:** `#tmpWorkflowElementsCustomFields` = `{cf_id: {element_id: {is_show, ...}}}`
  (π.χ. ροή 39: 6–12, όλα `is_show=1`).
  Τα κουμπιά add/remove της σελίδας είναι όλος ο κατάλογος (35) και δεν δείχνουν τι είναι κολλημένο.
  Ο χρήστης: όλοι οι σταθμοί βλέπουν όλα τα πεδία.
- **Αποθήκευση με κολλημένα πεδία:** `elementCustomFields` = `{cf: {mandatorycheckbeforeproduction, elements: {page_id: {show, check, …}}}}`.
  Το εργαλείο στέλνει όλα τα κολλημένα πεδία με ρυθμίσεις για ΟΛΑ τα βήματα (τα αποθηκευμένα κρατιούνται, τα νέα βήματα `show`).
  Με κενό `elementCustomFields` ο server κρατά τα κολλημένα πεδία όπως είναι (επαληθεύτηκε).
  Ροές με συνημμένα αρχεία: ακόμα απορρίπτονται.
- **Ids σταθμών:** ΕΙΣΑΓΩΓΗ 1, LASER 2, ΣΤΡΑΝΤΖΑ 3, ΜΟΝΤΑΖ 1 7, PUNCHING 8, ΨΥΚΤΙΚΑ 23, ΜΟΝΤΑΖ 2 27,
  ΚΟΠΗ ΨΑΛΙΔΙ 28, ΜΟΝΤΑΖ ΤΖΑΜΙΑ 31.
- **Ετικέτες (π.χ.):** ADMIN 25, ΣΧΕΔΙΟ (ΛΕΥΤΕΡΗΣ) 5, ΚΟΠΗ ΨΑΛΙΔΙ 6, PUNCHING 7, ΜΟΝΤΑΖ 10, ΠΟΛΥΟΥΡΕΘΑΝΗ 11,
  ΠΡΟΕΤΟΙΜΑΣΙΑ ΨΥΚΤΙΚΩΝ 13, ΕΓΚΑΤΑΣΤΑΣΗ ΨΥΚΤΙΚΩΝ 14, ΣΧΕΔΙΟ (ΘΑΝΑΣΗΣ) 15, ΤΖΑΜΙΑ ΠΑΡΑΓΓΕΛΙΑ 16,
  ΤΖΑΜΙΑ ΜΟΝΤΑΖ 17, ΗΛΕΚΤΡΟΛΟΓΙΚΑ 19, ΣΥΝΔΕΣΗ ΑΠΟΧΕΤΕΥΣΕΩΝ 20, ΣΥΝΑΡΜΟΛΟΓΗΣΗ 21, ΠΑΡΕΛΚΟΜΕΝΑ 22,
  ΣΤΡΑΤΖΑ 23, ΚΟΠΗ 24.

**Δοκιμή 1 (2/10, πέρασε):** δημιουργία #3203 με 3 βήματα, τροποποίηση (+ΨΥΚΤΙΚΑ, αλλαγή ετικέτας)
με διατήρηση ids, διαγραφή. 
**Δοκιμή 2 (2/10, πέρασε):** #3204 με πεδία 6 και 12 → + ΨΥΚΤΙΚΑ (πεδία ορατά και στα 4 βήματα) →
αλλαγή ετικέτας με κενά πεδία (κρατήθηκαν) → διαγραφή.
Πρότυπα ροών χωρίς συνημμένα αρχεία: επεξεργάσιμα. Επόμενο: ροές προϊόντων (`/product/{id}/workflow/{tpl}`).

## Ροή γραμμής εντολής στην παραγωγή (2026-10-02)
- **Τα προϊόντα δεν έχουν δικό τους αντίγραφο ροής:** δείχνουν απευθείας στο ΠΡΟΤΥΠΟ.
  Αλλαγή στο πρότυπο = αλλαγή για όλα τα προϊόντα του, στις ΜΕΛΛΟΝΤΙΚΕΣ γραμμές.
- **Κάθε γραμμή εντολής παίρνει ΔΙΚΗ της ροή** (τύπος `workorderline`, όνομα «workorderline: <κωδικός>»)
  τη στιγμή που ξεκινά/ολοκληρώνεται το πρώτο βήμα της.
  - Η αλλαγή κατάστασης σε «παραγωγή» ΔΕΝ αρκεί.
  - Μέχρι τότε η γραμμή δείχνει στο πρότυπο.
  - Στο αντίγραφο: όλες οι ενεργές γραμμές με δική τους ροή είχαν ξεκινημένα βήματα, όλες οι γραμμές σε πρότυπο ήταν σε αναμονή χωρίς ξεκινημένο βήμα.
- **Επεξεργασία:** στη σελίδα `/workorderlines/{κωδικός}/workflow/{ροή}`, με την ίδια αποθήκευση
  (`/workflow-update/{ροή}`) συν το πεδίο `workorderLine`. Εργαλείο: `WorkflowWriter.read_wol` / `update_wol`.
  - Βήματα με πρόοδο (`#workorderLineHits`: first_start / element_done / working_now) κλειδώνονται όπως στη σελίδα.
  - Γραμμή που δείχνει ακόμα σε πρότυπο → άρνηση (θα άλλαζε το πρότυπο για όλους).
- **Άλλες ενέργειες γραμμής:**
  - Αλλαγή κατάστασης: `POST /workorderline/set-status` (`id`, `status` = standby/production/archive).
  - Χειροκίνητη ολοκλήρωση βήματος: `POST /workorderline/zeroing-finish` (`workorderLineId`, `elementId`, `action=finish`).
  - Γραμμή που έχει ξεκινήσει δεν έχει κουμπί διαγραφής στη σελίδα εντολής → αρχειοθέτηση.

**Δοκιμή 3 (2/10, πέρασε):**
1. Προϊόν #1432 «ERMIS-TEST ροή γραμμής» (ανενεργό, πρότυπο 7), εντολή #754, γραμμή 3302.
2. Έναρξη παραγωγής: η γραμμή έμεινε στο πρότυπο.
3. Ολοκλήρωση του 1ου βήματος: φτιάχτηκε η ροή #3205.
4. Προστέθηκε 3ο βήμα: η γραμμή έχει 3 βήματα (επιβεβαίωση και από το API) και το πρότυπο 7 έμεινε ΑΝΕΓΓΙΧΤΟ.
5. Η γραμμή αρχειοθετήθηκε. Το προϊόν #1432 μένει ανενεργό (χρησιμοποιείται από την αρχειοθετημένη γραμμή).
6. Συνέχεια:
   - Η γραμμή βγήκε από το αρχείο (standby).
   - «Επανεργασία» του ολοκληρωμένου βήματος: `zeroing-finish` με `action=zero` μηδένισε την πρόοδο ΚΑΙ το ιστορικό εργασίας του βήματος.
   - Η σελίδα εντολής ξαναέδειξε τη γραμμή ως διαγράψιμη.
   - Διαγραφή γραμμής: η άδεια εντολή #754 χάθηκε μαζί της.
   - Διαγραφή προϊόντος #1432.
   - Δεν έμεινε τίποτα.
   **Κανόνας:** σε πραγματικές γραμμές ο μηδενισμός σβήνει χρόνους/ποιος/πότε (στατιστικά, «ολοκληρώθηκαν σήμερα»). Μόνο για λάθη, με έγκριση του χρήστη.

## Αλλαγές σε υπάρχοντα στοιχεία (2026-10-03) — `epoptia_edits.py`
- **Γραμμή:** `POST /workorderlines/update/{id}` (_method=PUT) με ένα από `description` / `qty` / `completion_date` (ΗΗ-ΜΜ-ΕΕΕΕ).
  - Σχόλια: AJAX στο ίδιο URL.
  - Ειδικά πεδία: AJAX `/workorderline/customfields` με ΟΛΑ τα πεδία (τα αμετάβλητα ξαναστέλνονται).
- **Εντολή:**
  - Ημερομηνία: `/workorder-change-completion-date`.
  - Πελάτης: `/workorders/updateclient/{id}`.
  - Σχόλια: AJAX `/workorders/update/{id}`.
- **Προϊόν:** όνομα με `/products/update/{id}`, σχόλια με AJAX.
- **Συμπεριφορές:**
  - Η ημερομηνία γραμμής ↔ εντολής ενημερώνεται και προς τις δύο κατευθύνσεις.
  - Μετά από AJAX save το Epoptia απαντά 302 (ο browser το ακολουθεί).
- **Ροή γραμμής:** αλλαγή ΣΤΑΘΜΟΥ σε υπάρχον βήμα αγνοείται από το Epoptia (αλλάζει μόνο η ετικέτα).
  - Γίνεται αντικατάσταση: νέο βήμα + ξανά οι συνδέσεις του (`replace_changed_stations`).
  - Η ολοκλήρωση του 1ου βήματος γραφείου βάζει τη γραμμή σε «production».
  - **Κανόνας χρήστη:** σε αλλαγή σταθμού, ΡΩΤΑ για κάθε βήμα αν η ετικέτα μένει ίδια ή αλλάζει
    (π.χ. PUNCHING «ΣΗΜΑΔΕΜΑ» → LASER «;»). Πρότεινε ετικέτες που έχει ο νέος σταθμός σε άλλες ροές.

**Δοκιμή 4 (3/10, πέρασε):**
- Προϊόν #1433, επαφές #390 «ERMIS-TEST 2» και #387, εντολή #755, γραμμή 3303.
- Αλλαγές γραμμής / εντολής / προϊόντος επαληθεύτηκαν.
- Στη 3303 οι δύο PUNCHING έγιναν LASER μόνο στη ροή της (#3206). Το πρότυπο 5 έμεινε ανέγγιχτο.

## Αντικατάσταση ροής προϊόντος (2026-10-03)
- Ίδια φόρμα με την πρώτη ανάθεση (`/product/workflow/from-template`).
  Εργαλείο: `assign_workflow(pid, νέα, replace_from=τρέχουσα)`.
- Αρνείται αν το προϊόν έχει αποθηκευμένες τιμές σε ειδικά πεδία (το Epoptia προειδοποιεί ότι χάνονται).
  Οι τιμές ανά γραμμή δεν επηρεάζονται.
- Δοκιμή: #1434 41→39, ίδια πεδία 6–12, μετά διαγραφή.
- Πραγματική χρήση: **#624 ΒΧΜ 70: 41 (ΑΚΥΡΟ) → 39 v.2**.
- Γραμμή 3171 (τρέχει): το βήμα LASER «ΤΖΑΜΙΑ ΜΟΝΤΑΖ» → ΜΟΝΤΑΖ ΤΖΑΜΙΑ (ίδια ετικέτα), μόνο στη ροή της.
  Τα 11 ολοκληρωμένα βήματα ανέγγιχτα. Οι 39 και 41 ανέγγιχτες.

## Ειδικά πεδία (custom fields) και checkboxes ανά βήμα (2026-10-03)
- Λίστα: `GET /customfields?per_page=100&cf_type=all&term=<όνομα>` → `<div class="deleteCustomfieldInfo">` JSON ανά πεδίο.
- Νέο πεδίο: φόρμα `#customFieldForm` (το action το βάζει το script: `/customfields/store`), πεδία
  `name, name_second, category (characteristics|procedures|controls), code, group, type
  (text|dropdown|checkbox|limit|value_equal|equation|bom), is_active, edit_rule, …`. Τα checkboxes της SARIDIS
  είναι `type=checkbox`, `category=procedures`. Εργαλείο: `WorkflowWriter.create_custom_field()`.
- Διαγραφή: `#customfieldDeleteForm` → `POST /customfields/destroy/{id}` (`_method=DELETE`), `delete_custom_field()`.
- **ΔΕΝ χρησιμοποιούμε** τον «κανόνα καθολικά» (`/custom-fields/apply-rule`, `/add-field-to-workflows`):
  βάζει το πεδίο σε ΟΛΕΣ τις γραμμές/ροές. Το πεδίο συνδέεται ανά ροή.
- Ανά ροή και ανά βήμα: `#tmpWorkflowElementsCustomFields` = `{field:{element_id:{is_show,is_check,…}}}`.
  `is_check=1` = ο σταθμός πρέπει να τσεκάρει το πεδίο για να κλείσει το βήμα (π.χ. LASER: ΠΑΤΟΣ ΕΞΩ κ.λπ.).
  Αποθήκευση μέσα στο `elementCustomFields` της ροής: `{field:{mandatorycheckbeforeproduction, elements:{"ermis-<step>":{show,check}}}}`.
  Βήμα που δεν αναφέρεται = κρυφό. Εργαλείο: `update(..., step_settings={field:{step:{"show":True,"check":True}}})`
  (και `create(..., step_settings=…)`); `{}` για ένα βήμα = αφαίρεση από αυτό το βήμα.
- Διόρθωση 2026-10-03: σε αποθήκευση, υπάρχον βήμα όπου ένα πεδίο ήταν κρυφό μένει κρυφό
  (πριν γινόταν ορατό· καμία πραγματική ροή δεν επηρεάστηκε — όλες οι μέχρι τότε αποθηκεύσεις είχαν μόνο πεδία ορατά παντού).
- Κανόνας SARIDIS: ο ψυκτικός (σταθμός 23) δεν έχει checkboxes, εκτός από το νέο «ΣΥΝΔΕΣΗ ΑΠΟΧΕΤΕΥΣΗΣ» στο βήμα ΕΓΚΑΤΑΣΤΑΣΗ ΨΥΚΤΙΚΩΝ.

## Ετικέτες εργασίας (job tags) (2026-10-03)
- Λίστα: `GET /tags?per_page=100` → `<div class="deleteTagInfo">` JSON (`id, name, section`).
- Μετονομασία: `#tagForm` → `POST /tags/update/{id}` με `_method=PUT, name, section` (τα `section[]` είναι disabled).
  Αλλάζει το όνομα παντού (ροές, γραμμές σε παραγωγή, ιστορικό). Εργαλείο: `WorkflowWriter.rename_tag()`.
- Νέα ετικέτα: `#tagsCreateForm` → `POST /tags/store` (δεν έχει χρησιμοποιηθεί ακόμα).
- 2026-10-03: η ετικέτα 15 «ΣΧΕΔΙΟ (ΘΑΝΑΣΗΣ)» μετονομάστηκε σε «ΣΧΕΔΙΟ» (απόφαση χρήστη).

## Σταθμοί (workstations) (2026-10-03)
- Λίστα: `GET /workstations?per_page=100` → `<div class="workstationInfo">` JSON (όλες οι ρυθμίσεις + tags).
- Επεξεργασία: `#workstationForm` → `POST /workstations/update/{id}` (`_method=PUT`), στέλνει ΟΛΕΣ τις ρυθμίσεις
  (`name, workstation_code, workstationTags[], max_works, folder_id, cost_per_hour, tracking_info, group_by_value,
  info_button_visible, edit_not_unique_tasks`). `rename_workstation()` ξαναστέλνει τις τρέχουσες τιμές και ελέγχει μετά ότι δεν άλλαξαν.
- Διαγραφή: `#workstationDeleteForm` → `POST /workstations/destroy/{id}` (`_method=DELETE`), `delete_workstation()`.
- Νέος σταθμός: `#addNewWorkstationForm` → `POST /workstations/store` (δεν έχει χρησιμοποιηθεί ακόμα).
- 2026-10-03 (απόφαση χρήστη): σβήστηκε ο αχρησιμοποίητος σταθμός 30 «ADMIN»· ο σταθμός 1 «ΕΙΣΑΓΩΓΗ ΠΑΡΑΓΓΕΛΙΑΣ»
  μετονομάστηκε σε «ADMIN»· η ετικέτα 25 «ADMIN» μετονομάστηκε σε «ΕΓΚΡΙΣΗ ΠΑΡΑΓΓΕΛΙΑΣ».
- Νέα ετικέτα: `create_tag(name)` → `POST /tags/store` με `section[]=workflow_elements` (ετικέτα βημάτων ροής).
- Νέος σταθμός: `create_workstation(name)` → `POST /workstations/store` με τις ρυθμίσεις όλων των σταθμών SARIDIS
  (max_works 100, μέτρηση χρόνου, χωρίς ομαδοποίηση/κόστος/φάκελο/ετικέτες).
- 2026-10-03: νέος σταθμός 32 «ΗΛΕΚΤΡΟΛΟΓΟΣ» και νέα ετικέτα 27 «LED» (απόφαση χρήστη).
