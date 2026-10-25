## Code text

The text of a code file is a `document` (`doc:<repo>:<path>`) cut into
`document_chunk` records, one per function or class, each with
`start_line`, `end_line` and `symbol`. Reading the text of a `file:` node
points to its document. Quote code from the chunk and cite path and lines.
