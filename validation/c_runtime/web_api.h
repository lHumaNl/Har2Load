#ifndef SYNTHETIC_WEB_API_H
#define SYNTHETIC_WEB_API_H
#include "lrun.h"
#define SEARCH_FILTERS "SEARCH_FILTERS"
#define RAW_BODY_START "RAW_BODY_START"
#define RAW_BODY_END "RAW_BODY_END"
int web_set_max_html_param_len(const char *);
int web_reg_save_param_ex(const char *, ...);
int web_custom_request(const char *, ...);
int web_add_header(const char *, const char *);
int web_set_option(const char *, ...);
int web_cleanup_cookies(void);
int web_add_cookie(const char *);
int web_cache_cleanup(void);
#endif
