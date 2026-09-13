#pragma once

#include <CLI/CLI.hpp>
#include <iostream>

namespace {
#define PARSE(app, argc, argv)                                                                     \
  do {                                                                                             \
    CLI11_PARSE(app, argc, argv);                                                                  \
  } while (0);
} // namespace
