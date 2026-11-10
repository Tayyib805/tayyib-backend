\echo '--- I1 import: one halal-marked, one candidate, one that matches an existing place (Al Amir)'
select import_places('[
 {"external_id":"ov-1","name":"Halal Guys","lat":33.80,"lng":-117.92,"city":"Anaheim","halal":true,"cuisine":["middle_eastern"]},
 {"external_id":"ov-2","name":"Kareem''s Falafel","lat":33.81,"lng":-117.93,"city":"Anaheim","halal":false,"phone":"7145550111"},
 {"external_id":"ov-3","name":"Al-Amir Restaurant","lat":33.8354,"lng":-117.9146,"halal":true,"website":"https://alamir.example"},
 {"external_id":"","name":"No id","lat":1,"lng":1}
]'::jsonb, 'overture', 'OC');
select name, status, tier, confidence, website from places where name in ('Halal Guys','Kareem''s Falafel','Al Amir Restaurant') order by name;
\echo '--- I2 same import again: all seen, nothing added'
select import_places('[
 {"external_id":"ov-1","name":"Halal Guys","lat":33.80,"lng":-117.92,"halal":true},
 {"external_id":"ov-2","name":"Kareem''s Falafel","lat":33.81,"lng":-117.93,"halal":false},
 {"external_id":"ov-3","name":"Al-Amir Restaurant","lat":33.8354,"lng":-117.9146,"halal":true}
]'::jsonb, 'overture', 'OC');
\echo '--- I3 admin sorts the candidate as halal -> on the map; map query'
insert into place_evidence (place_id, attribute, value, source, source_ref) select id, 'halal', 'yes', 'admin', 'admin-1' from places where name = 'Kareem''s Falafel';
select name, status, tier, confidence from places where name = 'Kareem''s Falafel';
select name, tier, round(distance_m) as m from places_near(33.8353, -117.9145, 8000) order by distance_m limit 6;
\echo '--- I4 review queue reasons'
select reason, count(*) from places_needing_review group by 1 order by 1;
