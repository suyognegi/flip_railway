import datetime
import json
from playwright.async_api import async_playwright
import asyncio
from fastapi import FastAPI, Query
from fastapi.responses import StreamingResponse

app = FastAPI()
mr = 0
nf = 0
mf = 0
pf = 0


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


async def scrape_reviews(url, sort_type, stability_count, review_limit, queue):
    try:
        t1 = datetime.datetime.now()

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()

            await page.goto(url, wait_until="networkidle")
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
                #print(sort_type, height)
                dat.append(height)
                print(sort_type, counter, datetime.datetime.now())
                print(sort_type, dat)
                try:
                    if dat[-1] > 2:
                        query = """
                            ()=>{
                                try {
                                    let ele = [...document.getElementsByClassName('lQLKCP')[0].children]
                                        .slice(6, -4)
                                        .at(-1);

                                    for (let i = 0; i <= 10; i++) {
                                        ele = ele.children[0];
                                    }

                                    let head_node = ele.children[0];

                                    let rating = parseFloat(
                                        head_node.children[1].textContent.slice(0, 3)
                                    ).toFixed(1);

                                    let text_review = ele.children[2].innerText;

                                    let last = ele.children[ele.children.length - 1];

                                    let bottom_first = last.children[0].textContent.split(',');

                                    let name = bottom_first[0].trim();
                                    let city = (bottom_first[1] || "").trim();

                                    let ago = last.children[2]
                                        .children[0]
                                        .children[1]
                                        .textContent
                                        .split(' · ')[1];

                                    return `${rating} ★ | ${text_review} | ${name} | ${city} | ${ago}`;

                                } catch (error) {
                                    return "";
                                }
                            }
                        """

                        final_data = await page.evaluate(query)

                        # ─── WAS: print(final_data)     # yield this ───
                        await queue.put({
                            "status": final_data,
                            "sort": sort_type,
                        })

                except:
                    # ─── WAS: print('NA ★ | NA | NA | NA | NA')   # if error ocur yield this ───
                    await queue.put({
                        "status": "NA ★ | NA | NA | NA | NA",
                        "sort": sort_type,
                    })

                counter += 1

            #print(f'starting 2nd {datetime.datetime.now()}')

            query = """
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

            final_data = await page.evaluate(query)
            #print(f'finished {datetime.datetime.now()}')

            await browser.close()

            return final_data

    except Exception as e:
        #print(f"Error in {sort_type}: {str(e)}")
        return []


async def worker(key, url, stability_count, review_limit, queue):
    try:
        result = await scrape_reviews(url, key, stability_count, review_limit, queue)
        await queue.put({"__terminal__": "DONE", "sort": key, "data": result})
    except Exception as e:
        await queue.put({"__terminal__": "ERROR", "sort": key, "error": str(e)})


@app.get('/')
def info():
    return {
        'success': True, 'example': 'http://127.0.0.1:8001/reviews?url=https://www.flipkart.com/ai-pulse-2-blue-128-gb/p/itmd59202944d081?pid=MOBHKHPYW49R78RA&lid=LSTMOBHKHPYW49R78RAR9LHZP&marketplace=FLIPKART&q=ai-pulse-2-blue-128-gb%2F&store=tyy%2F4io&srno=s_1_1&otracker=search&otracker1=search&fm=organic&iid=485458bc-4214-459c-82e3-3b63347acbde.MOBHKHPYW49R78RA.SEARCH&ppt=None&ppn=None&ssid=q8ehxrj8ao0000001781377954524&qH=7e67c8d1d8952871&ov_redirect=true',
        'example2': 'http://127.0.0.1:8001/reviews?url=https://www.flipkart.com/cadbury-dairy-milk-shots-chocolate-balls-truffles/p/itm961397bf05e15?pid=CHCFP7FHA3CCQHSQ&lid=LSTCHCFP7FHA3CCQHSQKKYGRI&marketplace=FLIPKART&q=chocolate&store=eat%2F0pt&srno=s_1_2&otracker=search&otracker1=search&fm=Search&iid=da4a7518-d52a-4c5a-9867-ffee409ae327.CHCFP7FHA3CCQHSQ.SEARCH&ppt=sp&ppn=sp&qH=c378985d629e99a4&ov_redirect=true&ov_redirect=true&const_alpha=12&limit=93',

    }


@app.get("/reviews")
async def get_reviews(url: str = Query(...), const_alpha: int = Query(8, ge=3, le=20), limit: int = Query(93, ge=0, le=1500)):
    urls_ = create_urls(url)
    keys = list(urls_.keys())
    t1 = datetime.datetime.now()
    queue: asyncio.Queue = asyncio.Queue()

    async def event_gen():
        tasks = [
            asyncio.create_task(worker(k, urls_[k], const_alpha, limit, queue))
            for k in keys
        ]

        results = {}
        finished = 0
        total = len(keys)

        try:
            while finished < total:
                ev = await queue.get()

                if "__terminal__" in ev:
                    if ev["__terminal__"] == "DONE":
                        results[ev["sort"]] = ev["data"]
                    else:
                        results[ev["sort"]] = []
                    finished += 1
                    continue

                yield json.dumps(ev) + "\n"

            aa = []
            x = []
            for i, j in enumerate(keys):
                a = {}
                r_ = results.get(j, [])
                a[j] = r_
                x.extend(r_)
                a["count"] = len(r_)
                aa.append(a)

            yield json.dumps({
                'success': True,
                'data': aa,
                'count': len(x),
                'time': (datetime.datetime.now() - t1).seconds,
                'speed': f'{len(x)/(datetime.datetime.now()-t1).seconds:.1f} rev/sec'
            }) + "\n"

        except Exception as e:
            import traceback
            traceback.print_exc()
            yield json.dumps({
                "success": False,
                "message": str(e),
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


# uvicorn anshul_alptop:app --port 8001
# if __name__ == "__main__":
#     import uvicorn
#     uvicorn.run(app, host="0.0.0.0", port=8001)
