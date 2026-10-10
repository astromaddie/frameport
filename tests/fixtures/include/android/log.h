#pragma once

#include <stdarg.h>

#define ANDROID_LOG_INFO 4
int __android_log_print(int priority, const char *tag, const char *format, ...);
int __android_log_vprint(int priority, const char *tag, const char *format, va_list args);
