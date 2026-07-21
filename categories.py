"""TikTok Shop category ID → display name.

TikTok's creator-search response tags each creator with `category_ids`, but
our app's single scope (seller.creator_marketplace.read) cannot call the Get
Categories product API, so the mapping is pinned here as static data.

Every entry below was confirmed against TikTok's own public storefront pages
(shop.tiktok.com/<region>/c/<slug>/<id>) on 2026-07-22 — the numeric IDs are
global across regions, only the display names are localized. Unknown IDs pass
through as the raw ID string: never invent a name, never hide a category.
"""

CATEGORY_NAMES = {
    "600001": "Home Supplies",
    "600024": "Kitchenware",
    "600154": "Textiles & Soft Furnishings",
    "600942": "Household Appliances",
    "601152": "Womenswear & Underwear",
    "601303": "Muslim Fashion",
    "601352": "Shoes",
    "601450": "Beauty & Personal Care",
    "601739": "Phones & Electronics",
    "601755": "Computers & Office Equipment",
    "602118": "Pet Supplies",
    "602284": "Baby & Maternity",
    "603014": "Sports & Outdoor",
    "604206": "Toys & Hobbies",
    "604453": "Furniture",
    "604579": "Tools & Hardware",
    "604968": "Home Improvement",
    "605248": "Fashion Accessories",
    "700437": "Food & Beverages",
    "700645": "Health",
    "801928": "Books, Magazines & Audio",
    "802184": "Kids' Fashion",
    "824328": "Menswear & Underwear",
    "824584": "Luggage & Bags",
}


def name(category_id):
    cid = str(category_id).strip()
    return CATEGORY_NAMES.get(cid, cid)


def names(category_ids):
    return [name(c) for c in (category_ids or [])]
