from datetime import datetime

from hypovisor.food.estimator import parse_quick_text
from hypovisor.food.knowledge_base import find_matches, get_or_create_food, learn_from_item, similarity
from hypovisor.models import Meal, MealItem


def test_quick_text():
    e = parse_quick_text("pizza 2 slices 60g carbs")
    assert e.total_carbs_g == 60 and "pizza" in e.meal_name
    assert parse_quick_text("just pizza") is None
    assert parse_quick_text("carbs: 42").total_carbs_g == 42


def test_similarity_and_matching(session):
    f = get_or_create_food(session, "chicken burrito bowl", carbs_g=60, provenance="user")
    get_or_create_food(session, "oatmeal with banana", carbs_g=55, provenance="user")
    assert similarity("chicken burrito bowl", "Chicken Burrito Bowl") > 0.95
    m = find_matches(session, "burrito bowl with chicken")
    assert m and m[0][0].id == f.id
    assert get_or_create_food(session, "Chicken burrito bowl ").id == f.id  # near-duplicate collapses
    assert not find_matches(session, "sushi")


def test_learn_from_item_corrections(session):
    meal = Meal(ts=datetime(2026, 1, 1, 12), utc_offset_min=-300, status="confirmed")
    item = MealItem(
        name="pizza slice",
        carbs_g=30,
        fat_g=10,
        protein_g=12,
        confidence=0.5,
        values_source="model",
        portion_desc="1 slice",
    )
    meal.items.append(item)
    session.add(meal)
    session.flush()
    food = learn_from_item(session, item, user_corrected=False)
    assert food.provenance == "model" and food.carbs_g == 30 and food.times_eaten == 1
    item2 = MealItem(
        name="pizza slice", carbs_g=38, fat_g=11, protein_g=12, values_source="user", portion_desc="1 slice"
    )
    meal.items.append(item2)
    session.flush()
    food2 = learn_from_item(session, item2, user_corrected=True)
    assert (
        food2.id == food.id
        and food2.carbs_g == 38
        and food2.provenance == "corrected"
        and food2.times_eaten == 2
    )
    # a later model estimate does not overwrite a corrected food
    item3 = MealItem(name="pizza slice", carbs_g=25, confidence=0.9, values_source="model")
    meal.items.append(item3)
    session.flush()
    assert learn_from_item(session, item3, user_corrected=False).carbs_g == 38
