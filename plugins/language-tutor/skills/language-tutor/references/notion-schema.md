# Notion schema (v1) — created live 2026-07-10

Parent page: "Language Tutor" (found at runtime via Notion search; fetch it to get all database IDs).
Creation order matters: Courses → Sections → Lessons → Vocabulary → Session Log → then ADD COLUMN relations on Courses.

## Courses
"Name" TITLE, "Target Language" RICH_TEXT, "Instruction Language" RICH_TEXT, "Level" RICH_TEXT,
"Status" SELECT('Active':green,'Paused':yellow,'Completed':blue), "Started" DATE
+ added after other DBs exist: "Current Section" RELATION(Sections), "Current Lesson" RELATION(Lessons)
+ auto: "Sections" (DUAL back-ref). Page body = learner profile/goals/preferences.

## Sections
"Name" TITLE, "Course" RELATION(Courses, DUAL 'Sections'), "Order" NUMBER,
"Status" SELECT('Skeleton':gray,'Expanded':blue,'In Progress':yellow,'Done':green),
"Objectives" RICH_TEXT, "Eval Score" NUMBER (0-100)

## Lessons
"Name" TITLE, "Section" RELATION(Sections, DUAL 'Lessons'), "Course" RELATION(Courses),
"Order" NUMBER, "Status" SELECT('Planned':gray,'In Progress':yellow,'Done':green),
"Score" NUMBER (0-100), "Grammar Points" RICH_TEXT. Page body = full lesson plan.

## Vocabulary
"Word" TITLE, "Romanization" RICH_TEXT, "Meaning" RICH_TEXT (instruction lang), "Example" RICH_TEXT,
"Course" RELATION(Courses), "Lesson" RELATION(Lessons, DUAL 'Vocabulary'),
"Next Review" DATE, "Interval" NUMBER (days), "Ease" NUMBER (start 2.5, min 1.3),
"Reps" NUMBER, "Lapses" NUMBER, "Status" SELECT('New':gray,'Learning':yellow,'Mature':green,'Suspended':red)

## Session Log
"Name" TITLE, "Course" RELATION(Courses), "Date" DATE,
"Type" SELECT('Setup':purple,'Planning':blue,'Lesson':yellow,'Vocab':orange,'Evaluation':red),
"Summary" RICH_TEXT, "Next Step" RICH_TEXT

## Notes
- Dates are set via expanded props: date:<Prop>:start, date:<Prop>:is_datetime.
- Checkboxes: "__YES__"/"__NO__". Relations query as JSON arrays of page URLs.
- SQL queries supported via notion-query-data-sources.
- Session boot = fetch Course row + latest Session Log entry. Batch writes at session close.
- Relation values on create/update: pass the target page URL (JSON array string for multiple). If a write is rejected, fetch the data source and follow its shown format.
- notion-create-pages: max 100 pages per call — chunk larger batches.
- SQL (notion-query-data-sources; relation columns only resolve after "view"-ing the related data source). Filter relations with LIKE on the page ID:
  - Due cards: SELECT url,"Word","Romanization","Meaning","Example","Interval","Ease","Reps","Lapses" FROM "collection://<vocab>" WHERE "Course" LIKE '%<course-page-id>%' AND "Status" IN ('Learning','Mature') AND "date:Next Review:start" <= date('now') ORDER BY "date:Next Review:start" LIMIT 20
  - Latest log: SELECT * FROM "collection://<log>" WHERE "Course" LIKE '%<course-page-id>%' ORDER BY "date:Date:start" DESC LIMIT 1
- Session Log naming convention: "YYYY-MM-DD — <Type>".
