-- SPDX-License-Identifier: Apache-2.0
module

-- These deliberately redundant imports make the trim example interesting.
public import Init.Data.Nat.Basic
public import Init.Data.List.Basic

@[expose] public section

namespace Hello

def greeting : String := "Hello, Lean!"

theorem two_plus_two : 2 + 2 = 4 := by decide

theorem greeting_nonempty : greeting ≠ "" := by decide

end Hello
