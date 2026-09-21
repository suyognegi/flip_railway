import asyncio
import datetime
import json
import traceback

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from playwright.async_api import async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

app = FastAPI()

NAV_TIMEOUT_MS = 60000  # Railway is slower than a laptop, 30s default is too tight
SPOOF_UA = True         # normal Chrome user agent + India locale (set False to disable)


def create_urls(url):
    # clean ot
    if '/product-reviews/' not in url:
        st = url.split('/p/')[0]
        itm = 'itm' + url.split('itm')[1].split('&marketplace=FLIPKART')[0] + '&marketplace=FLIPKART'
        url = f'{st}/product-reviews/{itm}'

    urls = {}
    ketchfra = ['MOST_HELPFUL', 'MOST_RECENT', 'POSITIVE_FIRST', 'NEGATIVE_FIRST']
    for i in ketchfra:
        if i in url:
            url = ''.join(url.split(i))
    for i in ketchfra:
        urls[i] = f'{url}&sortOrder={i}'

    return urls


async def keep_scrolling(lst, stability_count, review_limit, sort_type):
    if len(lst) < stability_count:
        return True
    return len(set(lst[-stability_count:])) != 1 and lst[-1] < (review_limit + 8)


# ---------------------------------------------------------------
# YOUR ORIGINAL EXTRACTION JS (unchanged)
# ---------------------------------------------------------------
FINAL_QUERY = """
                ()=>{
                    let final_data=[];
                    for (const [idx,v] of [...document.getElementsByClassName('lQLKCP')[0].children].slice(6,-4).entries()) {
                        let ele = v;

                        for (let i = 0; i <= 10; i++) {
                            console.log(i, ele);

                            if (!ele || !ele.children || !ele.children[0]) {
                                console.log("Broke at level", i);
                                break;
                            }

                            ele = ele.children[0];
                        }

                        let head_node = ele.children[0];
                        let rating = head_node.children[1].textContent.slice(0, 3);
                        let head_review = head_node.children[2].textContent;
                        let review_for = ele.children[1].textContent;
                        let text_review = ele.children[2].innerText; 

                        let last = ele.children[ele.children.length - 1];
                        let bottom_first = last.children[0].textContent.split(',');
                        let name = bottom_first[0];
                        let city = bottom_first[1] || "";

                        let updown = last.children[1].children[0];
                        let up = updown.children[0].textContent;
                        let down = updown.children[1].textContent;

                        if (up.includes('Helpful for')){
                            up = up.split('Helpful for ')[1];
                        } else {
                            up = '0';
                        }

                        if (down == ''){
                            down = '0';
                        }

                        let ago = last.children[2].children[0].children[1].textContent.split(' · ')[1];

                        let media_list = []
                        if (ele.children.length == 5){
                            let media = ele.children[3];
                            for (let i = 0; i <= 6; i++) {
                                media = media.children[0];
                            }
                            for (const i of media.children){
                                media_list.push(i.querySelector('img').src);
                            }
                        }

                        let temp_data = {
                            rating: rating,
                            head_review: head_review,
                            review_for: review_for,
                            text_review: text_review,
                            name: name,
                            city: city,
                            helpful: up,
                            not_helpful: down,
                            ago: ago,
                            media: media_list
                        };

                        final_data.push(temp_data);
                    }

                    return final_data;
                }
            """


async def scrape_reviews(p, url, sort_type, stability_count, review_limit, queue):
    """One sort order = its own browser. All four are started at the same moment."""
    browser = None
    try:
        await queue.put({"__debug__": f"[{sort_type}] launching browser"})

        # --- deployment-only settings (nothing to do with scrolling) ---
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage"],  # Docker/Railway has a tiny /dev/shm
        )

        if SPOOF_UA:
            major = browser.version.split(".")[0]
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
                ),
                locale="en-IN",
                timezone_id="Asia/Kolkata",
                viewport={"width": 1280, "height": 720},
            )
            page = await context.new_page()
        else:
            page = await browser.new_page()

        # networkidle can be slow/never fire on a server: if it times out, carry on anyway
        try:
            await page.goto(url, wait_until="networkidle", timeout=NAV_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            await queue.put({"__debug__": f"[{sort_type}] networkidle timeout, continuing anyway"})
        await queue.put({"__debug__": f"[{sort_type}] page loaded"})

        # If Flipkart served something else (block / captcha / error page), say exactly what
        try:
            await page.wait_for_selector(".lQLKCP", timeout=20000)
        except Exception:
            title = await page.title()
            body = await page.evaluate(
                "document.body ? document.body.innerText.slice(0, 300).replace(/\\s+/g, ' ') : ''"
            )
            raise RuntimeError(
                f"reviews container not found | title={title!r} | url={page.url!r} | body={body!r}"
            )

        # ---------------------------------------------------------------
        # YOUR ORIGINAL SCROLL LOGIC (unchanged)
        # ---------------------------------------------------------------
        await page.mouse.wheel(0, 2000)

        bottom = 1500
        dat = []
        counter = 1

        await page.mouse.move(562, 584)
        await page.mouse.click(562, 584)

        while await keep_scrolling(dat, stability_count, review_limit, sort_type):
            await page.mouse.wheel(0, bottom)
            bottom = bottom + 1500
            await page.mouse.wheel(0, -400)

            height = await page.evaluate("document.getElementsByClassName('lQLKCP')[0].children.length")
            dat.append(height)
            print(sort_type, counter, datetime.datetime.now())
            print(sort_type, dat)
            # keeps the streaming connection alive (no extra DOM work, just the number above)
            await queue.put({"status": f"{sort_type}: {height} items loaded", "sort": sort_type})
            counter += 1

        await queue.put({"__debug__": f"[{sort_type}] final extraction"})
        final_data = await page.evaluate(FINAL_QUERY)
        return final_data

    except Exception as e:
        # no silent failures: the client sees why this sort order died
        await queue.put({"__debug__": f"[{sort_type}] FATAL: {type(e).__name__}: {e}"})
        traceback.print_exc()
        return []

    finally:
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass


async def worker(p, key, url, stability_count, review_limit, queue):
    try:
        result = await scrape_reviews(p, url, key, stability_count, review_limit, queue)
        await queue.put({"__terminal__": "DONE", "sort": key, "data": result})
    except Exception as e:
        await queue.put({"__terminal__": "ERROR", "sort": key, "error": str(e)})


@app.get('/')
def info():
    return {
        'success': True,
        'example': 'http://127.0.0.1:8001/reviews?url=https://www.flipkart.com/ai-pulse-2-blue-128-gb/p/itmd59202944d081?pid=MOBHKHPYW49R78RA&marketplace=FLIPKART&limit=93',
        'example2': 'http://127.0.0.1:8001/reviews?url=https://www.flipkart.com/cadbury-dairy-milk-shots-chocolate-balls-truffles/p/itm961397bf05e15?pid=CHCFP7FHA3CCQHSQ&marketplace=FLIPKART&const_alpha=12&limit=93',
    }


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.get("/reviews")
async def get_reviews(
    url: str = Query(...),
    const_alpha: int = Query(8, ge=3, le=20),
    limit: int = Query(93, ge=0, le=1500),
):
    try:
        urls_ = create_urls(url)
    except Exception:
        raise HTTPException(status_code=400, detail="Could not parse that Flipkart product URL")

    keys = list(urls_.keys())
    t1 = datetime.datetime.now()
    queue: asyncio.Queue = asyncio.Queue()

    async def event_gen():
        tasks = []
        try:
            async with async_playwright() as p:
                await queue.put({"__debug__": f"started, sorts={keys}"})

                # four tasks created together -> four browsers launch simultaneously
                tasks = [
                    asyncio.create_task(worker(p, k, urls_[k], const_alpha, limit, queue))
                    for k in keys
                ]

                results = {}
                finished = 0
                total = len(keys)

                while finished < total:
                    ev = await queue.get()

                    if "__terminal__" in ev:
                        if ev["__terminal__"] == "DONE":
                            results[ev["sort"]] = ev["data"]
                        else:
                            results[ev["sort"]] = []
                        finished += 1
                        await queue.put({"__debug__": f"[{ev['sort']}] done ({finished}/{total})"})
                        continue

                    yield json.dumps(ev) + "\n"

                # flush anything still waiting in the queue
                while not queue.empty():
                    ev = queue.get_nowait()
                    if "__terminal__" not in ev:
                        yield json.dumps(ev) + "\n"

                aa = []
                x = []
                for j in keys:
                    a = {}
                    r_ = results.get(j, [])
                    a[j] = r_
                    x.extend(r_)
                    a["count"] = len(r_)
                    aa.append(a)

                elapsed = max(1, (datetime.datetime.now() - t1).seconds)

                yield json.dumps({
                    'success': True,
                    'data': aa,
                    'count': len(x),
                    'time': elapsed,
                    'speed': f'{len(x) / elapsed:.1f} rev/sec'
                }) + "\n"

        except Exception as e:
            traceback.print_exc()
            yield json.dumps({
                "success": False,
                "message": f"{type(e).__name__}: {e}",
                "data": [],
                "all_data": [],
                "count": 0,
                "time": 0,
                "speed": 0
            }) + "\n"

        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()

    return StreamingResponse(event_gen(), media_type="application/x-ndjson")


"""
adjust the const_alpha variablee low means less reviews hence faster
high the value more the reviews hence slower (advixe keep it max to 12 (have successfully extracted 1576 total reviewed reviews with speed 13/sec) )
for best/avg results keep it to 6
personally note havig limit is good (fast) and gnerally generates result 2.8x*limit ~ 4*limit
"""

# run locally: uvicorn main:app --port 8001
