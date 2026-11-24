# Agento — etykiety tooli MCP, stubowanie i ostrzeżenia *lethal trifecta*

PRD techniczne · 23 listopada 2026  
Podstawa analizy: `openagento/agento`, gałąź `main`, commit [`65bc169`](https://github.com/openagento/agento/commit/65bc169aebb9a3d10d3bb0fc9424788789e0a850).

## 1. Cel i zakres

Admin może sklasyfikować toole MCP w jednym miejscu, zobaczyć pełną *lethal trifecta* podczas konfiguracji agenta i ocenić, czy kombinacja była dostępna podczas konkretnego runu. Autor ewaluacji może stubować grupę tooli wskazaną etykietą.

Zakres obejmuje katalog i edycję etykiet, dwa ostrzeżenia dla admina, flagę `--stub-tools` oraz migrację ustawień `sensitive`. `--exclude-tools`, blokowanie zapisu lub wykonania z powodu trifecty, edycja masowa i odtwarzanie klasyfikacji historycznych to osobne zadania poza tym wydaniem.

## 2. Punkty integracji zweryfikowane w kodzie

| Obszar | Stan w analizowanym commicie |
| --- | --- |
| Panel tooli | `Tools.tsx` używa `EnablementTree`, która zapisuje konfigurację w paczce i trzyma niezapisane zmiany osobno dla każdego scope. `GET /api/admin/tools` czyta skatalogowane deklaracje modułów przez `get_tool_states`. [Tools.tsx](https://github.com/openagento/agento/blob/65bc169aebb9a3d10d3bb0fc9424788789e0a850/frontend/panel/src/routes/admin/Tools.tsx#L7-L18) · [EnablementTree.tsx](https://github.com/openagento/agento/blob/65bc169aebb9a3d10d3bb0fc9424788789e0a850/frontend/panel/src/routes/admin/EnablementTree.tsx#L25-L70) · [admin_api.py](https://github.com/openagento/agento/blob/65bc169aebb9a3d10d3bb0fc9424788789e0a850/src/agento/web/admin_api.py#L224-L229) · [data.py](https://github.com/openagento/agento/blob/65bc169aebb9a3d10d3bb0fc9424788789e0a850/src/agento/framework/admin/data.py#L667-L730) |
| Dostęp w Toolboxie | Rejestr używa deklaracji, `requires` i konfiguracji scope. Dispatcher ponownie sprawdza dostęp i argumenty przed stubem lub właściwym wykonaniem. Toole niedostępne i granice capability wchodzą do oceny dostępności. [config-loader.js](https://github.com/openagento/agento/blob/65bc169aebb9a3d10d3bb0fc9424788789e0a850/src/agento/toolbox/config-loader.js#L807-L878) · [dispatcher.js](https://github.com/openagento/agento/blob/65bc169aebb9a3d10d3bb0fc9424788789e0a850/src/agento/toolbox/dispatcher.js#L93-L144) |
| Zewnętrzne MCP | Claude i Codex mogą dostać własne połączenia bezpośrednio z `agent_view/mcp/servers`. Te wywołania omijają dispatcher Toolboxa. Playwright jest przykładem zewnętrznego MCP opakowanego przez Toolbox. [Claude config](https://github.com/openagento/agento/blob/65bc169aebb9a3d10d3bb0fc9424788789e0a850/src/agento/modules/claude/src/config.py#L586-L631) · [Codex config](https://github.com/openagento/agento/blob/65bc169aebb9a3d10d3bb0fc9424788789e0a850/src/agento/modules/codex/src/config.py#L425-L444) · [playwright-client.js](https://github.com/openagento/agento/blob/65bc169aebb9a3d10d3bb0fc9424788789e0a850/src/agento/toolbox/playwright-client.js#L86-L111) |
| Tryb testowy | `--test` przyjmuje JSON z `stub` i `real`. Słowo `real` ma dziś pierwszeństwo nad domyślnym `sensitive`. Toolbox czyta politykę z capability. Tryb testowy zachowuje obecny warunek środowiskowy i nie przekazuje klucza SSH runowi. [run.md](https://github.com/openagento/agento/blob/65bc169aebb9a3d10d3bb0fc9424788789e0a850/docs/cli/run.md#L52-L85) · [test_run.py](https://github.com/openagento/agento/blob/65bc169aebb9a3d10d3bb0fc9424788789e0a850/src/agento/framework/test_run.py#L31-L78) · [toolbox_capability.py](https://github.com/openagento/agento/blob/65bc169aebb9a3d10d3bb0fc9424788789e0a850/src/agento/framework/toolbox_capability.py#L116-L122) |
| Dziedziczenie i konwersacje | Subagent i job wznawiający rodzica przenoszą `test_config`. Konwersacja ma rekord `execution` osobny dla każdej próby i odczytuje runy przez REST. Dostęp do szczegółów runu może mieć też użytkownik bez roli admina. [agent.js](https://github.com/openagento/agento/blob/65bc169aebb9a3d10d3bb0fc9424788789e0a850/src/agento/modules/subagent/toolbox/agent.js#L92-L149) · [observers.py](https://github.com/openagento/agento/blob/65bc169aebb9a3d10d3bb0fc9424788789e0a850/src/agento/modules/subagent/src/observers.py#L135-L150) · [hooks.py](https://github.com/openagento/agento/blob/65bc169aebb9a3d10d3bb0fc9424788789e0a850/src/agento/modules/conversation/src/hooks.py#L63-L90) · [routes.py](https://github.com/openagento/agento/blob/65bc169aebb9a3d10d3bb0fc9424788789e0a850/src/agento/modules/conversation/src/routes.py#L134-L151) |

## 3. Katalog i klasyfikacja

| Stały klucz | Etykieta | Kryterium przypisania |
| --- | --- | --- |
| `private-data` | Dostęp do danych prywatnych | Tool może udostępnić agentowi dane niepubliczne w konfiguracji tego połączenia. |
| `untrusted-content` | Treść z niezaufanego źródła | Tool może dostarczyć do kontekstu agenta treść kontrolowaną przez niezaufaną stronę, np. wiadomość od zewnętrznego nadawcy. |
| `external-communication` | Komunikacja na zewnątrz | Tool pozwala przekazać dane poza zaufaną granicę, np. w wiadomości lub żądaniu z danymi kontrolowanymi przez agenta. |

Definicje trzech etykiet są wbudowane i chronione przed zmianą oraz usunięciem. Panel może tłumaczyć ich nazwy. Klucze pozostają niezależne od języka. Admin może zmieniać przypisania. Etykieta techniczna `stub-in-test` ma zarezerwowany klucz dla zgodności `--test`. Proponuję chronić jej definicję przed usunięciem i umożliwić edycję przypisań.

Tożsamość toola to stabilny identyfikator źródła lub połączenia oraz dokładna nazwa toola u źródła. Alias widoczny dla harnessu jest polem katalogu. Dwie instancje tego samego serwera mają odrębne przypisania. Ponowne wykrycie tej samej pary odzyskuje przypisania. Zmiana nazwy toola u dostawcy tworzy nowy wpis. Nieobecny tool zachowuje przypisania i nie wchodzi do bieżącej oceny.

Agento dostarcza wersjonowane przypisania dla wbudowanych tooli. Lokalne dodanie lub zdjęcie etykiety zastępuje wartość domyślną dla tej pary tool–etykieta. Własne moduły i połączenia zewnętrzne klasyfikuje admin. Przegląd ma dwa stany: „Do sprawdzenia” i „Sprawdzony”. Nowe toole zaczynają od pierwszego. Admin może zatwierdzić pusty zestaw etykiet ryzyka. Sama edycja własnego chipa nie zatwierdza przeglądu.

Przegląd dotyczy wersji klasyfikacji toola. Porównanie wersji uwzględnia publiczny kontrakt toola i jawne pola konfiguracyjne wpływające na klasyfikację, np. zmianę połączenia lub środowiska. Restart i identyczne odświeżenie katalogu zachowują wynik. Istotna zmiana przywraca „Do sprawdzenia” i zachowuje przypisania. Gdy dostawca zmienia zachowanie przy identycznym kontrakcie, admin może ręcznie przywrócić ten status. Wersja i dane porównawcze nie zawierają sekretów.

## 4. Panel i kontrakt API

- **Katalog tooli.** Rozszerzyć ekran **Tools** o listę pokazującą nazwę, źródło, etykiety i status przeglądu. Admin wyszukuje po nazwie, filtruje po źródle, statusie i etykietach. Proponowane zachowanie filtrów to dopasowanie wszystkich wybranych etykiet oraz połączenie ich z pozostałymi filtrami.
- **Edycja toola.** Dostępne są podpowiedzi istniejących chipów i tworzenie własnej etykiety podczas przypisania. Zapisać utworzenie i przypisanie w jednej transakcji. Własne etykiety mają unikatową nazwę po normalizacji i stabilny klucz CLI pokazywany w panelu. Zmiana nazwy nie zmienia klucza. Usuwanie poprzedza podgląd liczby powiązań i potwierdzenie. Zapis usuwa powiązania w tej samej transakcji.
- **Edycja `agent_view`.** Rozszerzyć obecne drzewo tooli o etykiety i podgląd ryzyka. Liczyć z aktualnego szkicu, łącznie z dziedziczeniem scope oraz zależnościami `requires`. Filtr i zwinięcie grupy nie wpływają na wynik. Czerwony komunikat wskazuje toole w każdej kategorii. Widoczność i zapis uprawnień dalej korzystają z obecnej ścieżki konfiguracji.
- **Konwersacja.** Czerwony komunikat należy do próby wykonania. Występuje przy odpowiednim runie po odświeżeniu danych i po ponownym otwarciu konwersacji. Backend wysyła pole tylko adminowi, również gdy zwykły użytkownik ma uprawnienie do szczegółów runu. Nie publikować klasyfikacji w treści rozmowy ani w ogólnym strumieniu zdarzeń.

Proponowany kontrakt rozszerza istniejący odczyt tooli i wykorzystuje obecny mechanizm sesji, ACL i CSRF:

| Operacja | Kontrakt |
| --- | --- |
| `GET /api/admin/tools` | Zachować podział na toolsety i dotychczasowe pola. Dla admina dodać identyfikator katalogowy, źródło, efektywne etykiety, status i wersję klasyfikacji. |
| `GET/POST /api/admin/tool-labels` | Lista definicji i utworzenie własnej etykiety. |
| `PATCH/DELETE /api/admin/tool-labels/{id}` | Zmiana nazwy lub usunięcie własnej etykiety. Liczbę powiązań zwraca odczyt. Odrzucić zmianę chronionej definicji. |
| `PATCH /api/admin/tools/{id}/classification` | Zapis przypisań, opcjonalnego nowego chipa i decyzji o przeglądzie. Konflikt wersji zwraca `409` i pozwala odświeżyć edytowany wpis. |
| Szczegóły konwersacji | Opcjonalne pole w istniejącym rekordzie runu. Dla admina obejmuje wynik, stan pokrycia i nazwy tooli według kategorii. Brak danych dla starych runów. |

## 5. Rozpoznawanie *lethal trifecta*

Reguła to sprawdzenie, czy suma etykiet zakwalifikowanych tooli zawiera wszystkie trzy stałe klucze. Może je zapewnić jeden tool, dwa toole albo trzy i więcej. Etykiety przy statusie „Do sprawdzenia” liczą się od razu. W edytorze wejściem jest zestaw tooli w szkicu, po rozstrzygnięciu zależności i dziedziczenia. W konwersacji wejściem jest zestaw realnie dostępny dla capability przy starcie próby, po odjęciu efektywnie stubowanych tooli. Użyć obecnych reguł dostępu Toolboxa, w tym ograniczeń capability i grantów tam, gdzie mają zastosowanie. Nie dodawać nowych uprawnień za pomocą etykiet.

Dla runu z niepełnym katalogiem źródeł zapisać ocenę jako niepełną. Jeśli znane toole już pokrywają trzy kategorie, nadal wyświetlić czerwone ostrzeżenie. W pozostałych przypadkach niepełne pokrycie powinno być widoczne dla admina jako odrębny stan. Zapisać wynik oraz nazwy pasujących tooli według kategorii raz dla próby. Późniejsza zmiana etykiet i per-call cofnięcie uprawnienia nie modyfikują wyniku z chwili startu.

## 6. Zewnętrzne połączenia MCP

Wniosek z kodu: samo dodanie etykiet do obecnej listy modułów nie spełni wymagań dla bezpośrednich połączeń. **Proponowana decyzja architektoniczna** to jawne połączenia zarządzane przez Toolbox. Toolbox odkrywa toole i udostępnia je przez swój rejestr, z osobnym ID połączenia, jednoznacznymi aliasami, kontrolą enablement per tool i wywołaniem przez istniejący dispatcher. Metadane trafiają do panelu bez danych uwierzytelniających. Dodanie serwera i sama operacja discovery nie włączają automatycznie jego tooli. Zewnętrzne deklaracje nie nadają sobie wbudowanych etykiet.

Przed implementacją zinwentaryzować formy konfiguracji i transportów używane przez wspierane harnessy. Potwierdzić ścieżkę migracji dla każdego wspieranego typu połączenia. Po przeniesieniu usunąć równoległą konfigurację bezpośredniego połączenia dla tego samego źródła. Dla wdrożeń przejściowych zachować obecny dostęp bezpośredni i pokazać jego wpływ na pokrycie oceny. Run z nową flagą ma zgłosić błąd przed startem, jeśli ma aktywne połączenie bezpośrednie, którego tooli nie da się objąć deklarowanym stubowaniem. Dotychczasowe `--test` zachowuje dotychczasowy zakres. Test wydania ma potwierdzić katalogowanie, ostrzeżenia i stubowanie zewnętrznego toola przez zarządzane połączenie. Jeżeli któregoś wspieranego typu nie da się zmigrować, jest to luka w uzgodnionym zakresie do rozstrzygnięcia przed wydaniem.

## 7. CLI, stubowanie i migracja

```bash
agento run eval_01 --stub-tools finanse --stub-tools external-communication
```

- Każda flaga przyjmuje jeden stabilny klucz. Powtórzenia oznaczają sumę dopasowań. Flaga sama włącza tryb testowy i współdziała z istniejącym JSON-em `--test`. Nieznana etykieta zatrzymuje start. Znana etykieta bez pasujących tooli daje zero dodatkowych stubów. Przyjąć obecne pozycje flag względem kodu widoku i promptu.
- Wynik to suma jawnego `stub`, dopasowań `--stub-tools` i domyślnego `stub-in-test` pomniejszonego o `real`. Obecny konflikt jawnego `stub` z `real` pozostaje błędem. Jawne dopasowanie przez `--stub-tools` w konflikcie z `real` również zatrzymuje start i wskazuje tool.
- Rozwiązać etykiety do konkretnych ID raz przy starcie runu. Zapisać wersjonowaną listę w istniejącej ścieżce polityki runu i capability. Przekazać ją bez ponownego rozwiązywania do subagentów i wznowienia rodzica. Lista ma uwzględniać skatalogowane toole także poza widokiem rodzica, aby dziecko uruchomione w innym dozwolonym widoku odziedziczyło te same stuby. Obecny tryb testowy nadal wymaga `AGENTO_TEST_MODE_ENABLED` i zachowuje istniejące reguły klucza SSH.
- Wspólny dispatcher po autoryzacji i walidacji argumentów zwraca dotychczasowe `{"ok": true}`, pomija handler i zachowuje `tool_invocation.stubbed = 1`. Stub nie zmienia listy narzędzi. Błędna polityka testowa zatrzymuje wykonanie, aby tool nie wykonał realnej operacji wskutek błędu konfiguracji.
- Migrować scalone domyślne `tools/<name>/sensitive: "1"` na `stub-in-test`, także w modułach spoza repo. Stare `"0"` i brak wartości w importerze oznaczają brak etykiety. Kod wbudowanych modułów przechodzi na nowe deklaracje. Walidator dopuszcza stare manifesty w okresie zgodności, a loader tłumaczy stary klucz na wejściu. Usunąć `registry.isSensitive` z rejestru i dispatchera. Istniejące konfiguracje JSON, zapisane testowe joby i `job:set-runtime --test` nadal działają. Przed aktualizacją opróżnić aktywne runy testowe albo wykazać zgodność obu formatów na czas przełączenia.

## 8. Model danych, placement i rozszerzenia

| Jednostka | Dane i właściciel |
| --- | --- |
| Katalog i etykiety | Framework, Python. Proponowane tabele `tool_catalog`, `tool_label`, `tool_label_assignment` ze stabilnym kluczem źródła i toola, wersją przeglądu oraz unikatową parą tool–etykieta. Lokalne przypisanie przechowuje jawne dodanie lub zdjęcie. Razem z wersjonowanymi domyślnymi danymi modułów stanowi źródło wspólne dla panelu i runtime. |
| Połączenia MCP | Node, Toolbox i moduł będący właścicielem połączenia. Rozszerzenie ogólnego rejestru pozostaje niezależne od dostawcy. Wsparcie wymaga zgodności deklaracji, ACL i granicy poświadczeń. |
| Rozwiązanie polityki testowej | Framework i adapter istniejącej komendy `run` w `agent_view` po stronie Python. Node wykonuje decyzję przez istniejący dispatcher. |
| Wynik próby | Wspólna ocena używa metadanych dostępności z Toolboxa. Moduł `conversation` zapisuje projekcję przy rekordzie `execution` i odpowiada za odczyt admin-only. Proponowane opcjonalne pole JSON jest małe, obejmuje stan oceny i toole stanowiące podstawę ostrzeżenia. Użyć widoczności i cyklu usuwania rekordu próby. |
| UI i autoryzacja | Istniejące Web API dla katalogu i `agent_view`, moduł `conversation` dla konwersacji. Test roli admina na serwerze jest wymagany także przy bezpośrednim żądaniu API. |

Placement opiera się na PLC-1/3 i MOD-1/3. Rejestr jest współdzielonym kontraktem frameworka, a integracje zachowują własne moduły. Używać migracji repo dla zmian schematu i danych. Zmiany klasyfikacji zapisywać atomowo i odczytywać wsadowo, bez zapytania na tool w pętli i bez dodatkowego odczytu etykiet przy każdym wywołaniu (CODE-8, SCL-1). Granica poświadczeń oraz kontrola uprawnień per call pozostają częścią implementacji (SEC-1/5/7/9, TBX-1/2/3).

**Extension points (EVT-1/2/8).** Wspólny serwis Pythona zapisuje zmianę przypisania i przejście stanu przeglądu. Dla tych zmian nie ma zdarzenia w pierwszym wydaniu, ponieważ wszystkie trzy odbiorniki czytają stan bezpośrednio i nie ma konkretnego niezależnego obserwatora. Gdy konfiguracja tooli jest zapisywana przez istniejące API enablement, zachować dotychczasowe zdarzenia tej ścieżki. Zapis wyniku próby jest metadanymi runu przy istniejącym cyklu `execution`. Nie wymaga nowego zdarzenia.

## 9. Kolejność implementacji i warunki odbioru

1. **Kontrakty i migracja.** Katalog, stałe klucze, lokalne nadpisania, wersje przeglądu, mapowanie `sensitive`. Zweryfikować zgodność na wbudowanych i przykładowym zewnętrznym module.
2. **Zewnętrzne MCP.** Przeprowadzić próbę migracji i dowieść, że poszczególne połączenia mają rozłączne tożsamości. Sprawdzić dostępność katalogu przed startem runu oraz moment zapisu wyniku względem `run.started`. Jeśli wynik powstaje po pierwszym odczycie REST, powiązać jego zapis z istniejącym odświeżaniem szczegółów i sprawdzić widoczność ostrzeżenia w aktywnej konwersacji.
3. **Panel i rozpoznawanie.** Sprawdzić dodawanie oraz użycie chipa na drugim toolu, zmianę nazwy i usunięcie/globalną liczbę powiązań, niezmienność definicji systemowych, nadpisania po aktualizacji i przegląd po zmianie kontraktu. W edytorze przetestować komplet trzech etykiet, brak jednej, komplet na jednym toolu i zależność `requires`. W konwersacji sprawdzić dwa runy tego samego wątku z różnymi wynikami, brak dostępu dla użytkownika bez roli admina, status niepełnej oceny i stare runy.
4. **CLI i runtime.** Sprawdzić wielokrotne flagi, obie pozycje względem kodu widoku, nieznany klucz, konflikt z `real`, nadpisanie stuba domyślnego, wyłączony tryb testowy, dziedziczenie w innym widoku oraz zachowanie listy po zmianie etykiety w trakcie runu. Potwierdzić, że handler stubowanego toola nie został wywołany, istnieje wpis audytu, a taki tool nie wpływa na ostrzeżenie danego runu.
5. **Dokumentacja i weryfikacja.** Uzupełnić dokumenty CLI, tworzenia tooli i architektury panelu oraz `DECISIONS.md` o tożsamość toola, zgodność `--test` i źródło oceny przy starcie runu. Zgodnie z TST-1 wykonać testy ścieżek Python, Toolbox i panel, następnie `bin/test`. To PRD opisuje plan zmian. Wyników testów implementacji jeszcze nie ma.

## Ustalenia z rozmowy (23.11.2026)

- Pytania 1–12 zatwierdziły wspólny katalog, edycję przez admina, etykiety wielokrotnego użytku, zakres połączenia, lokalne nadpisania i ponowny przegląd.
- „3 wbudowane labelki są niezmienialne, nieusywalne” określa kontrakt definicji systemowych.
- „Nie dawajmy exclude tools, yagni” zamyka zakres tej flagi. „Niech zwracają OK tak jak teraz” określa wynik stuba.
- „Chce zobaczyć ostrzeżenie na czerwono w edycji tooli dla danego agent view oraz w konwersacji - ale tylko jako admin” określa dwa miejsca i widoczność.
- Zatwierdzenie pytań 16–23 wybrało `--stub-tools`, zgodność `--test`, ocenę konfiguracji na żywo, wynik przy starcie runu, konflikt z `real`, dziedziczenie oraz jawny przegląd. Propozycje nazw technicznych, modelu API i obsługi bezpośrednich MCP są decyzjami projektowymi tego PRD.
