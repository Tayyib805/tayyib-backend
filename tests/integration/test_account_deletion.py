"""Deleting an account (what DELETE /users/me asks Supabase to do) with rows in every newer table."""


def count(pg, sql):
    return pg.run(f"select to_json(count(*)) from ({sql}) t")


def test_deleting_an_account_removes_personal_rows_and_unlinks_contributions(pg, people):
    person, other = people(), people()
    uid = person["id"]
    place = pg.run(
        "with p as (insert into public.places (name, lat, lng, status, created_by) "
        f"values ('Deletion Test Grill', 12.5, 12.5, 'active', '{uid}') returning id) select to_json(id) from p"
    )
    ingredient = pg.run(
        "with i as (insert into public.ingredients (name, default_status) values ('deletion test ingredient', 'uncertain') returning id) select to_json(id) from i"
    )
    for sql in [
        f"insert into public.place_evidence (place_id, attribute, value, source, source_ref, user_id) values ('{place}', 'halal', 'yes', 'user', md5('{uid}'), '{uid}')",
        f"insert into public.place_evidence (place_id, attribute, value, source, source_ref, user_id) values ('{place}', 'halal', 'yes', 'user', md5('{other['id']}'), '{other['id']}')",
        f"insert into public.place_reviews (place_id, user_id, rating, body) values ('{place}', '{uid}', 5, 'Great')",
        f"insert into public.place_flags (place_id, user_id, reason) values ('{place}', '{uid}', 'wrong_location')",
        f"insert into public.place_claims (place_id, user_id, method) values ('{place}', '{uid}', 'admin')",
        f"insert into public.subscription_events (id, user_id, type) values ('evt-delete-test', '{uid}', 'INITIAL_PURCHASE')",
        f"insert into public.ingredient_rulings (ingredient_id, madhab, status, reasoning, source, created_by) values ('{ingredient}', 'hanafi', 'halal', 'Entered for a deletion test only.', 'Test source', '{uid}')",
    ]:
        pg.run(f"with x as ({sql} returning 1) select to_json(count(*)) from x")

    before = pg.run(f"select row_to_json(t) from (select tier, confidence from public.places where id = '{place}') t")
    assert before["tier"] == "confirmed", "two shoppers confirmed it"

    pg.run(f"with x as (delete from auth.users where id = '{uid}' returning 1) select to_json(count(*)) from x")

    assert count(pg, f"select 1 from public.user_profiles where id = '{uid}'") == 0
    assert count(pg, f"select 1 from public.scan_logs where user_id = '{uid}'") == 0
    assert count(pg, f"select 1 from public.place_reviews where place_id = '{place}'") == 0, "their review goes with them"
    assert count(pg, f"select 1 from public.place_claims where place_id = '{place}'") == 0

    kept = pg.run(f"select json_agg(user_id) from public.place_evidence where place_id = '{place}' and source_ref = md5('{uid}')")
    assert kept == [None], "what they told us stays, no longer linked to them"
    assert pg.run(f"select to_json(created_by) from public.places where id = '{place}'") is None
    assert pg.run(f"select to_json(user_id) from public.place_flags where place_id = '{place}'") is None
    assert pg.run("select to_json(user_id) from public.subscription_events where id = 'evt-delete-test'") is None
    assert pg.run(f"select to_json(created_by) from public.ingredient_rulings where ingredient_id = '{ingredient}'") is None

    after = pg.run(f"select row_to_json(t) from (select status, tier from public.places where id = '{place}') t")
    assert after == {"status": "active", "tier": "confirmed"}, "the place and its label are unchanged"
