/* Included after the actual emitted helpers. Input is hex-encoded fixture data. */
static int hex_digit(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    assert(c >= 'a' && c <= 'f'); return c - 'a' + 10;
}
static unsigned char *decode(const char *s, size_t *length) {
    size_t i, n = strlen(s); unsigned char *out;
    assert(n % 2 == 0); *length = n / 2; out = malloc(*length + 1); assert(out);
    for (i = 0; i < *length; ++i) {
        out[i] = (unsigned char)(hex_digit(s[i * 2]) * 16 + hex_digit(s[i * 2 + 1]));
    }
    out[*length] = 0; return out;
}
static void print_hex(const char *s) {
    const unsigned char *p = (const unsigned char *)s;
    if (p) while (*p) printf("%02x", *p++);
}
int main(int argc, char **argv) {
    FILE *input; char *line, *fields[10], *cursor; int count;
    assert(argc == 2); input = fopen(argv[1], "r"); assert(input);
    line = malloc(4 * H2L_FIELD_LIMIT + 1024); assert(line);
    while (fgets(line, 4 * H2L_FIELD_LIMIT + 1024, input)) {
        unsigned char *body; size_t length; int parsed = -1;
        h2l_value value = {NULL, "ERROR_token", 0, 0};
        assert(strchr(line, '\n')); *strchr(line, '\n') = 0;
        /* Unlike strtok, retain empty fields. */
        count = 0; cursor = line; fields[count++] = cursor;
        while ((cursor = strchr(cursor, '\t'))) { *cursor++ = 0; assert(count < 10); fields[count++] = cursor; }
        assert(count >= 2); stub_reset(); h2l_transaction_failed = 0;
        body = decode(fields[1], &length);
        h2l_set(&value, "STALE", 5, 1, 0);
        if (!strcmp(fields[0], "J")) {
            h2l_json document; h2l_path path[70]; char *keys[70]; int path_count = 0;
            char *token = fields[2], *next;
            assert(count == 7);
            while (*token) {
                size_t ignored;
                next = strchr(token, ','); if (next) *next = 0;
                assert(path_count < 70);
                if (*token == 'k') {
                    keys[path_count] = (char *)decode(token + 1, &ignored);
                    path[path_count].key = keys[path_count]; path[path_count].index = 0;
                } else {
                    assert(*token == 'i'); keys[path_count] = NULL;
                    path[path_count].key = NULL; path[path_count].index = strtoul(token + 1, NULL, 10);
                }
                ++path_count; if (!next) break; token = next + 1;
            }
            alloc_attempts = 0; alloc_fail_at = atoi(fields[6]);
            h2l_json_parse(&document, (char *)body); parsed = !document.failed;
            h2l_json_select(&value, &document, path, path_count, strtoul(fields[3], NULL, 10), atoi(fields[4]));
            if (atoi(fields[5])) {
                h2l_value typed = {NULL, "ERROR_token", 0, 0};
                h2l_accept(&typed, &value, atoi(fields[5])); h2l_reset(&value); value = typed;
            }
            h2l_json_free(&document);
            while (path_count) free(keys[--path_count]);
        } else if (!strcmp(fields[0], "B")) {
            size_t ignored; char *left, *right;
            assert(count == 7); left = (char *)decode(fields[2], &ignored); right = (char *)decode(fields[3], &ignored);
            h2l_boundary(&value, (char *)body, left, right, strtoul(fields[4], NULL, 10), atoi(fields[5]), atoi(fields[6]));
            free(left); free(right);
        } else if (!strcmp(fields[0], "S")) {
            h2l_buffer buffer; int type = atoi(fields[2]); assert(count == 5);
            h2l_reset(&value);
            if (type) h2l_set(&value, (char *)body, length, type, atoi(fields[3]));
            h2l_open(&buffer, H2L_FIELD_LIMIT); h2l_slot(&buffer, &value, atoi(fields[4]));
            printf("S\t%d\t", h2l_transaction_failed); print_hex(h2l_text(&buffer, "FALLBACK"));
            printf("\t"); print_hex(value.data); h2l_reset(&value); test_free(buffer.data);
            printf("\t%d\n", live_allocations); free(body); continue;
        } else if (!strcmp(fields[0], "C")) {
            char *captured; assert(count == 4); response = body; response_size = length; eval_rc = atoi(fields[3]);
            captured = h2l_capture(atoi(fields[2])); h2l_reset(&value);
            if (captured) { h2l_set(&value, captured, strlen(captured), 1, 0); test_free(captured); }
            parsed = eval_calls * 10 + eval_frees;
        } else assert(0);
        printf("R\t%d\t%d\t%d\t%d\t%d\t", parsed, value.data != NULL, value.type, value.integral, h2l_transaction_failed);
        print_hex(value.data); h2l_reset(&value);
        printf("\t%d\n", live_allocations); free(body);
    }
    assert(!ferror(input)); fclose(input); free(line); return 0;
}
