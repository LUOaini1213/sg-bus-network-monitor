# Stops that changed in August 2026, and why

This page covers the surveillance in `src/demand.py`. For each stop it compares August 2026 weekday boardings per day with a baseline, and flags the stop if it moved far from the network-wide shift. `src/anomalies.py` then collects evidence for every flagged stop. `outputs/anomaly_evidence.csv` has the full table.

Each explanation below is marked in one of two ways:

- **Verified**: an official source (LTA or an operator notice, a university or MOE calendar) names the stop or its services and gives dates that match the data. The sources are listed in `data/reference/network_events_2026.csv`.
- **Consistent with**: the data pattern matches a known calendar, but no source names the stop itself.

## Choosing the baseline

The first version compared August with July alone. That flagged the four polytechnic areas as big drops (Ngee Ann Poly −37%, Nanyang Poly −32%, Republic Poly −28%, Temasek Poly −23%). The monthly series shows the real story: February, June and August are at the same level, and **July is the unusual month** (Ngee Ann Poly: 2,868 → 2,870 → 4,755 → 2,979 boardings a day).

The polytechnic calendars explain this. Teaching resumed on 29 June, so July was a full teaching month. August is late teaching and exams.

The surveillance now uses the **median of February, June and July** as its baseline. With that baseline:

- 67 surges and 4 drops are flagged, out of 2,932 stops with 300+ daily boardings;
- the polytechnic false alarms disappear;
- The Float, Blk 861 and Blk 745 are still flagged.

## What the 71 flags turn out to be

| Category | Stops | Status |
|---|---|---|
| Stop closed or services rerouted away | 3 | **Verified** (2 events) |
| New stops, which have no baseline and so are listed separately | 3 | **Verified** for 2 |
| Services added | 2 | The route change is verified for 1 (service 965), but its timing does not match the rise |
| University, polytechnic or ITE term, by stop name | 27 | Consistent with official calendars |
| Term pattern within 600 m of a campus | 12 | Consistent with official calendars |
| School-term seasonality (June holidays in the baseline) | 7 | Consistent with the MOE calendar |
| Sustained step up since July (Science Park II, The Alpha, Jurong West Ave 3) | 4 | No source found; still a lead |
| Service change of unclear direction (S36/S37 in April data only) | 2 | No official notice found |
| Unexplained | 14 | Still leads |

### Verified cases

**The Float @ Marina Bay (02051): 402 → 17 boardings a day, −96%.**
- LTA's notice closed the stop temporarily from **4 August to 12 October 2026** while the Formula 1 Grand Prix was being prepared. Fifteen services skip it.
- The September route data no longer lists any service at the stop, which matches the notice.
- This is a temporary closure. The stop should come back in October's data.

**The Jurong West St 75 reopening (19 July 2026): four stops.**
- The road had been partly closed since 7 December 2025, with services 181/181M, 243G/W, 258/258M, 502/502A and 651 diverted.
- When it reopened, those services returned to St 75, calling again at **27389 Gek Poh Shop Ctr** and **27381 Blk 749**. They stopped serving the detour stops.
- The data shows every piece of this:
  - Gek Poh and Blk 749 have no record in February or June. They appear part-way through July (225 and 112 boardings a day) and rise in August, the first full month (628 and 296).
  - **Blk 861** (27439) falls from 1,389 (June) to 1,108 (July) to 502 (August).
  - **Blk 745** (27349) falls from 1,351 to 1,048 to 634.
  - The route data confirms that those services left both stops between the April and September pulls.

**Service 965 (amended 24 May 2026).** The service now runs to Buangkok Int through Sengkang East and calls at Opp Blk 200B (67339). The route change is verified, but it **does not explain the timing**. Boardings at 67339 were flat in June (288 in February, 294 in June), after the change, and rose only in July and August (357, 382). The +30% flag therefore stays a lead.

**Renumbered services are not new services.** On 15 June the City Direct services 513, 868E, 951E, 982E and 850E became 646 to 650. The first evidence table showed "649 added" at Bukit Batok Fire Stn. That was only a new number, so the renumbering is now treated as no change.

### Consistent with academic and school calendars

- **University stops**: 27 by name (NTU Nanyang Dr, NUS Kent Ridge, UTown, SMU and ITE) plus 12 within 600 m of a campus.
  - NUS and NTU start teaching on 10 August, and SMU on 17 August. June and July fall in the vacation.
  - Lee Wee Nam Library at NTU, for example, runs 3,523 (February), 1,285 (June), 1,674 (July), 3,885 (August).
- **ITE College East (Simei)**: +40% against the baseline, which equals its July level. ITE's term started on 13 July, so August was its first full teaching month. February was higher again (2,084 against 1,858 in August).
- **Seven neighbourhood stops**: February and August match within 20%, while June is well below February.
  - MOE's June holidays ran from 30 May to 28 June.
  - This pattern is why a baseline made only of holiday months overstates the August "surge".

### Still leads

- **The four "sustained step up since July" stops** rose between June and July and have held there. Science Park II went 637 → 1,266 → 1,303. No official notice was found. A new tenant or site in the Science Park is a hypothesis that nothing here has tested.
- **The 14 unexplained stops** include Opp HarbourFront Int (−29% against the baseline), Blk 131D in Tengah and Blk 970A in Bedok Reservoir.
- **Sengkang East (Blk 305D, Renjong Stn Exit B)**: these jumped between February and June (140 → 840 boardings a day at Blk 305D) and have been flat since. No LTA notice or key-collection record was found. Services 80/86/87/372/374 moved temporarily to Compassvale Int from 31 January, but no source ties that to these stops.

None of these is explained until a source or a second month confirms it.
