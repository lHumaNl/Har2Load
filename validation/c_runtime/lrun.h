#ifndef SYNTHETIC_LRUN_H
#define SYNTHETIC_LRUN_H
/* Deliberate test contract, NOT SDK declarations or ABI evidence. */
#define LR_PASS 0
#define LR_FAIL 1
#define LR_AUTO 2
#define LR_ON_ERROR_CONTINUE 1
#define LAST ((const char *)0)
int lr_error_message(const char *, ...);
int lr_eval_string_ext(const char *, int, char **, unsigned long *, int, int, int);
void lr_eval_string_ext_free(const char **);
int lr_save_string(const char *, const char *);
int lr_continue_on_error(int);
int lr_start_transaction(const char *);
int lr_end_transaction(const char *, int);
#endif
