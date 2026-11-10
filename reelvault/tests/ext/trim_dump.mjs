// node tests/ext/trim_dump.mjs <fixture.json> <clips|feed>
// Prints what the extension would upload for a fixture page (bigint.js + trim.js), for test_igparse.py.
import {readFileSync} from "node:fs";
import {parseIg} from "../../extension-members/bigint.js";
import {trimMedia, isReel} from "../../extension-members/trim.js";

const [file, source = "clips"] = process.argv.slice(2);
const page = parseIg(readFileSync(file, "utf8"));
const items = source === "clips" ? (page.items || []).map(it => it && it.media).filter(Boolean)
                                 : (page.items || []).filter(isReel);
process.stdout.write(JSON.stringify(items.map(trimMedia)));
