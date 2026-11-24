import { cpSync } from "node:fs";

for (const folder of ["cmaps", "standard_fonts"]) {
  cpSync(new URL(`../node_modules/pdfjs-dist/${folder}/`, import.meta.url),
         new URL(`../dist/assets/pdfjs/${folder}/`, import.meta.url),
         { recursive: true });
}
