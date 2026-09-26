import { cp, mkdir, readdir, rm } from "node:fs/promises"
import { dirname, relative, resolve, sep } from "node:path"
import { fileURLToPath } from "node:url"

const project = resolve(dirname(fileURLToPath(import.meta.url)), "..")
const repository = resolve(project, "../../..")
const exported = resolve(project, "out")
const destination = resolve(repository, "gh0st/static/web")
const relativeDestination = relative(repository, destination)

if (
  relativeDestination.startsWith(`..${sep}`) ||
  relativeDestination === ".."
) {
  throw new Error("Refusing to copy the web export outside the repository")
}

const entries = await readdir(exported)
if (!entries.includes("index.html")) {
  throw new Error(`Next.js export is missing index.html: ${exported}`)
}

await rm(destination, { recursive: true, force: true })
await mkdir(destination, { recursive: true })
await cp(exported, destination, { recursive: true })
console.log(`Copied static gh0st UI to ${destination}`)
